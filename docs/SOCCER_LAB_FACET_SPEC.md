# Soccer Lab Frozen Facet Spec

This is the implementation spec for Soccer Lab facet-projector lenses. It is
derived from `docs/data/soccer_lab_column_facets.csv` and follows
`docs/STRUCTURAL_DATA_DOCTRINE.md`.

The machine-readable source of truth is
`docs/data/soccer_lab_facet_spec.json`.

## Registration

Each facet becomes one executable projector:

```bash
calyx add-lens <vault> \
  --name <facet> \
  --runtime external-cmd \
  --endpoint <exe> \
  --shape "Dense(<d>)" \
  --modality text
```

The executable parses the shared stat-line, emits exactly `d` finite floats, and
fails closed on malformed input.

## Team/Match Panel

Prediction target: `match_result`.

| Facet | Shape | Timing | Temporal Policy |
|---|---:|---|---|
| `attack` | `Dense(6)` | `ex_ante` | prior matches only |
| `defense` | `Dense(5)` | `ex_ante` | prior matches only |
| `tempo` | `Dense(4)` | `ex_ante` | prior matches only |
| `discipline` | `Dense(4)` | `ex_ante` | prior matches only |
| `pedigree` | `Dense(6)` | `ex_ante` | static/prior tournament only |
| `form` | `Dense(5)` | `ex_ante` | prior matches only |
| `context` | `Dense(8)` | `ex_ante` | current fixture allowed |

The `attack`, `defense`, `tempo`, `discipline`, and `form` facets may use
historical ex-post columns only after temporal shifting into prior-match rolling
aggregates. They must never read the current fixture's result, goals, penalties,
cards, or shootout fields.

## Player Panel

Prediction target: `player_impact`.

| Facet | Shape | Timing | Temporal Policy |
|---|---:|---|---|
| `output` | `Dense(5)` | `ex_ante` | prior matches only |
| `profile` | `Dense(7)` | `ex_ante` | static/prior tournament only |
| `efficiency` | `Dense(5)` | `ex_ante` | prior matches only |

The player facets use prior appearances, goals, penalties, and bookings only as
history before the target match. Static player profile columns can be used
directly.

`output` is an outcome-style facet over goals, starts, substitutions, and
penalties. In predictive vaults it is legal only after temporal shifting into
prior-match aggregates. Raw current-match output remains ex-post and must become
anchors or explanatory-only evidence.

## Verification

Regenerate no spec by hand. Validate it against the column map:

```bash
./tools/data/validate_facet_spec.py
```

Validation checks:

- every facet has a `Dense(d)` shape and `dense_dim == len(features)`;
- every referenced source column exists in `docs/data/soccer_lab_column_facets.csv`;
- predictive facets are `ex_ante`;
- ex-post source columns are allowed only with `prior_match_only` or
  `static_or_prior_tournament_only` temporal policies;
- required Soccer Lab facets are present.
