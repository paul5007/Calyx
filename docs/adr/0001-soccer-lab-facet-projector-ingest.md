# ADR 0001: Soccer Lab Uses Facet-Projector Ingest on Calyx

- Status: Accepted for Soccer Lab build work
- Date: 2026-07-03
- Issue: #13
- Binding reference: `docs/STRUCTURAL_DATA_DOCTRINE.md`
- Evidence revision: `ae856f9e266644dc5d570719434d0a1d0a4f38ea`

## Context

Soccer Lab is the FIFA World Cup prediction platform being built on Calyx. The
source signal is structured football data: teams, players, fixtures, match
context, historical results, and in-event outcome fields. The binding doctrine
states that structured/tabular data must use facet projectors, not general text
embedders or opaque line hashes.

The current foundation tooling is:

- `tools/data/acquire_soccer_lab_sources.py`: pulls raw Soccer Lab sources into
  `scratchpad/wc2026/raw/` and records byte-level acquisition evidence.
- `tools/data/generate_soccer_lab_rows.py`: turns real raw CSV/JSON sources into
  deterministic Calyx batch JSONL rows.
- `tools/data/provenance_manifest.py`: writes and verifies raw-source
  provenance manifests with bytes, SHA-256, and row counts.
- `tools/data/soccer_lab_sources.json`: declares the external source inventory.

At this revision, the real Fjelstul World Cup CSV/codebook sources are present
and verified. Kaggle, TheStatsAPI, and Hugging Face mirror acquisition remain
blocked on external credentials/source configuration tracked outside this ADR.

## Decision

Soccer Lab will ingest structured football data as Calyx constellations whose
prediction signal is exposed by external-command facet projector lenses.

Each facet lens is a single executable with a shebang and executable bit. It is
registered with Calyx using:

```bash
calyx add-lens <vault> \
  --name <facet> \
  --runtime external-cmd \
  --endpoint <exe> \
  --shape "Dense(<d>)" \
  --modality text
```

Every projector reads the same compact stat-line input and emits only its own
normalized numeric facet values. This keeps facet slots separate and avoids
flattening all structured columns into one text embedding.

Predictive panels must use only ex-ante facets:

- pedigree and priors
- trailing form
- match context
- fixture/tournament context

Ex-post facts must become anchors, not predictors:

- match result
- goals and goal difference
- xG and in-event statistics
- final standings or realized progression

The row generator already follows this split for historical match rows:

- `matches.jsonl` text excludes score/result fields and stores
  `label:match_result` anchors.
- `teams-history.jsonl` text excludes goals/result fields and stores
  `label:team_match_result` anchors.

## Architecture

The Soccer Lab data path is:

1. Acquire raw sources into `scratchpad/wc2026/raw/`.
2. Write a raw-source provenance manifest and verify it by rereading source
   bytes, SHA-256, and row counts.
3. Generate deterministic Calyx batch JSONL rows in `scratchpad/wc2026/rows/`.
4. Create Calyx vaults for teams, matches, and players.
5. Add at least two external-command facet projector lenses per predictive
   vault.
6. Ingest batch rows with metadata and grounded anchors.
7. Run `bits` per prediction axis.
8. Run `weave-loom`, `kernel-build`, `guard calibrate`, and
   `rebuild-search-index` in doctrine order.
9. Use Oracle readbacks for sufficiency, prediction, expansion, and reverse
   query.
10. Serve predictions through API endpoints and render them in the dashboard.

All source and derived writes must be verified against physical readback. A
return value is not evidence unless the bytes behind it were read.

## Consequences

This decision preserves the structural signal in the source data and keeps
prediction panels honest for future fixtures. It also means:

- projectors must fail closed on malformed, missing, or non-finite inputs;
- changing a projector's math creates a new frozen lens identity;
- no engine thresholds may be lowered to force a passing demo;
- no ex-post column may leak into a future-prediction facet;
- closure of downstream issues requires byte-level evidence, not only command
  success.

## Codebase-Memory Status

The required project name is `home-paula-projects-Calyx`. During this ADR update,
the codebase-memory MCP transport returned `Transport closed` for
`list_projects`, `index_status`, `get_architecture`, and `get_graph_schema`.
Therefore this ADR records the architectural decision, but #13 must remain open
until the MCP index can be freshly verified or rebuilt and the graph evidence is
posted to the issue.
