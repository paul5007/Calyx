# Soccer Lab Schema and Facet Map

This document records how Soccer Lab maps World Cup source columns into Calyx
facet-projector inputs. It follows `docs/STRUCTURAL_DATA_DOCTRINE.md`: lenses
are facet projectors, not embedders, and ex-post facts are anchors or
explanatory-only fields.

The complete column-level map is `docs/data/soccer_lab_column_facets.csv`. It is
generated from the physical Fjelstul codebook at
`scratchpad/wc2026/raw/fjelstul/codebook/variables.csv`.

## Timing Classes

- `ex_ante`: knowable before the match or tournament outcome. These columns may
  feed predictive facet projectors.
- `ex_post`: realized after or during the event. These columns must not feed
  future-prediction facets; use them as grounded anchors or explanatory-only
  slots.
- `metadata`: lineage, labels, URLs, and stable ids. These are retained for
  provenance, UI, joins, and FSV readback, but are not mathematical predictors.

## Facets

| Facet | Timing | Use |
|---|---:|---|
| `pedigree` | `ex_ante` | Team/player/person priors, roles, flags, and historical identity features. |
| `context` | `ex_ante` | Tournament, stage, group, date/time, venue, home/away, squad, and appointment context. |
| `outcome_anchor` | `ex_post` | Match result, score, standings, points, winner, and realized progression labels. |
| `event_outcome` | `ex_post` | Goals, cards, substitutions, penalties, appearances, awards, and other in-event observations. |
| `lineage` | `metadata` | Stable ids and human-readable names used for joins/readback. |
| `provenance` | `metadata` | Source URLs and links retained for traceability. |

## Primary Predictive Rows

The current row generator emits these pipeline rows:

| Output | Source table | Predictive text | Anchors |
|---|---|---|---|
| `players.jsonl` | `players.csv` | player id, name tokens, sex flag, role flags, tournament count | none |
| `matches.jsonl` | `matches.csv` | tournament id, match id, stage/group, date, venue, city/country, home/away team ids | `label:match_result` |
| `teams-history.jsonl` | `team_appearances.csv` | tournament id, match id, stage/group, date, team/opponent ids, home/away flags | `label:team_match_result` |
| `fixtures.jsonl` | TheStatsAPI fixture JSON | match id/number, competition, season, stage/group, kickoff, home/away ids | none until results arrive |
| `fjelstul.jsonl` | all Fjelstul CSV tables | raw explanatory archive rows | none |

The match and team-history outputs deliberately exclude score, goals, result,
penalties, and win/draw/loss fields from `text`. Those realized facts are
grounded anchors or explanatory-only fields.

## Normalization Policy

- Booleans: project as `0.0` or `1.0`.
- Counts and scores: normalize by bounded tournament-scale min/max when used in
  explanatory panels; realized scores/results are not allowed in predictive
  panels.
- Dates: convert to temporal features only when the date is ex-ante for the
  prediction target.
- Categorical fields: preserve as tokens for parsing; projectors may one-hot or
  ordinal-encode only within their facet.
- Stable ids and names: metadata/readback only unless a future issue explicitly
  promotes a derived statistic from them.
- Links: provenance only.

## Complete Map Generation

Regenerate and verify the complete map:

```bash
./tools/data/generate_schema_facet_map.py write
./tools/data/generate_schema_facet_map.py verify
```

Verification rereads the codebook and fails closed on missing input, missing
required codebook columns, or any map mismatch.
