# Soccer Lab Oracle Occurrence Contexts

This is the committed contract for Soccer Lab Oracle context JSON. It extends
`docs/STRUCTURAL_DATA_DOCTRINE.md` section 7 and keeps prediction-time facets
ex-ante while grounding outcomes through anchors and Oracle occurrence edges.

Machine-readable source of truth:
`docs/data/soccer_lab_oracle_context_schema.json`.

## Persisted Ingest Metadata

Every Soccer Lab row that participates in Oracle must carry an `oracle` object
in the batch JSONL:

```json
{
  "domain": "soccer_lab.match_result",
  "action": "predict_match_result",
  "outcome": "home_win",
  "outcome_kind": "label:match_result",
  "grounded": true,
  "t_secs": 1704067200
}
```

The ingest path must persist these metadata keys into the base constellation:

| Key | Meaning |
|---|---|
| `oracle.domain` | Stable Oracle domain id. |
| `oracle.action` | Stable action/action_id used by predict and expand readbacks. |
| `oracle.effect` | JSON encoded `AnchorValue` for the grounded outcome. |
| `oracle.structured` | Literal `true`, marking the row as structured Oracle evidence. |

Rows must also retain source metadata: `project`, `entity`, `source_dataset`,
`source`, and `source_key`. Entity-specific join keys are required where
available: `match_id`/`tournament_id` for fixtures, `team_id` for team rows, and
`player_id` plus `team_id` for player rows.

## PredictionContext JSON

`PredictionContext` is the recurrence context consumed by
`calyx readback oracle_predict`. It must contain:

```json
{
  "action_id": "predict_match_result",
  "outcome_anchor": { "value": { "enum": "home_win" } },
  "consequence": {
    "action_or_event": "predict_match_result",
    "domain": "soccer_lab.match_result",
    "outcome": { "value": { "enum": "home_win" } }
  }
}
```

`oracle_verdict` is accepted by the engine as a compatibility alias, but Soccer
Lab writes `outcome_anchor`. The persisted occurrence context stores `action_id`
only to stay under Calyx's 256-byte recurrence-context bound; the same action is
also persisted as base metadata in `oracle.action`.

## ExpansionContext JSON

`ExpansionContext` is the edge context consumed by
`calyx readback oracle_expand`. It uses the same action fields and one or more
grounded consequence edges:

```json
{
  "action_id": "predict_team_match_result",
  "consequences": [
    {
      "action_or_event": "predict_match_result",
      "domain": "soccer_lab.match_result",
      "outcome": { "value": { "enum": "home_win" } }
    }
  ]
}
```

Omitted grounding fields use the engine defaults: `grounded=true` and
`provisional=false`. An edge with explicit `grounded=false` or
`provisional=true` is not a trusted expansion edge. Unsupported axes must remain
unsupported; the Oracle honesty gate should return `Insufficient` rather than
fabricating a prediction.

## Domain Contracts

| Domain class | Domain id | Entity rows | Action | Outcome kind |
|---|---|---|---|---|
| Fixture | `soccer_lab.match_result` | `match`, `match_2026` | `predict_match_result` | `label:match_result` |
| Team | `soccer_lab.team_match_result` | `team_match_history` | `predict_team_match_result` | `label:team_match_result` |
| Team | `soccer_lab.tournament_winner` | `team_tournament` | `predict_tournament_winner` | `label:winner` |
| Team | `soccer_lab.tournament_finalist` | `team_tournament` | `predict_tournament_finalist` | `label:finalist` |
| Team | `soccer_lab.tournament_semi_finalist` | `team_tournament` | `predict_tournament_semi_finalist` | `label:semi_finalist` |
| Player | `soccer_lab.player_impact` | `player` | `predict_player_impact` | `label:player_impact` |

Fixture outcomes are `home_win`, `draw`, and `away_win`. Team match outcomes are
`win`, `draw`, and `lose`. Tournament progression outcomes (`winner`,
`finalist`, and `semi_finalist`) use `0` and `1`. Player impact is a v1 design
target; current generated player rows remain unanchored until a grounded
player-impact axis is added.

## Verification

Verify this contract against the committed schema, current Rust context parsers,
real generated WC rows, physical recurrence CF bytes, and synthetic known-input
edges:

```bash
./tools/data/verify_soccer_lab_oracle_context_format.py
```
