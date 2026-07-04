# Soccer Lab data acquisition

`acquire_soccer_lab_sources.py` pulls the raw source datasets for the Soccer Lab
World Cup pipeline into `scratchpad/wc2026/raw/`.

Required external credentials:

- Kaggle: install the `kaggle` CLI and set `KAGGLE_USERNAME` plus `KAGGLE_KEY`
  or provide `~/.kaggle/kaggle.json`.
- TheStatsAPI: set `THESTATSAPI_KEY`.
- Hugging Face mirror: set `SOCCER_LAB_HF_DATASET_REPO`; private mirrors also
  need `HF_TOKEN`.

The script fails closed. It does not substitute cached, synthetic, or fallback
data when a source is unreachable or credentials are missing. Every write is
read back from disk and recorded with bytes plus SHA-256 in
`acquisition_manifest.json`; structured failures are appended to
`acquire.log.jsonl`.

Examples:

```bash
./tools/data/acquire_soccer_lab_sources.py
./tools/data/acquire_soccer_lab_sources.py --only http_files
./tools/data/acquire_soccer_lab_sources.py --only kaggle --only thestatsapi
```

Generate deterministic Calyx batch JSONL rows:

```bash
./tools/data/generate_soccer_lab_rows.py
./tools/data/generate_soccer_lab_rows.py --only players --only matches --only matches-2026 --only teams-history --only team-tournaments --only players-2026 --only fjelstul
```

Outputs are written to `scratchpad/wc2026/rows/`:

- `players.jsonl`
- `matches.jsonl`
- `matches-2026.jsonl`
- `teams-history.jsonl`
- `team-tournaments.jsonl`
- `players-2026.jsonl`
- `fjelstul.jsonl`
- `fixtures.jsonl`

Match and team-history rows keep ex-post results out of `text`; outcomes are
stored only as grounded anchors.

Write and verify the raw-source provenance manifest:

```bash
./tools/data/provenance_manifest.py write
./tools/data/provenance_manifest.py verify
```

The manifest records every raw source file with bytes, SHA-256, row-count kind,
row count where applicable, source id, source kind, URL, and content type.
Verification recomputes those values from disk and exits nonzero on any mismatch.

Generate and verify the source-column facet map:

```bash
./tools/data/generate_schema_facet_map.py write
./tools/data/generate_schema_facet_map.py verify
```

The generated map is `docs/data/soccer_lab_column_facets.csv`; the narrative
schema documentation is `docs/SOCCER_LAB_SCHEMA_FACETS.md`.

Validate the frozen facet spec:

```bash
./tools/data/validate_facet_spec.py
```

The frozen spec is `docs/data/soccer_lab_facet_spec.json`; the narrative doc is
`docs/SOCCER_LAB_FACET_SPEC.md`.

Verify the team/match facet projector executables:

```bash
./tools/data/verify_team_match_projectors.py
```

The endpoint executables live in `tools/lenses/soccer_lab/team_match/`:
`attack`, `defense`, `tempo`, `discipline`, `pedigree`, `form`, and `context`.

Verify the player facet projector executables:

```bash
./tools/data/verify_player_projectors.py
```

The endpoint executables live in `tools/lenses/soccer_lab/player/`: `output`,
`profile`, and `efficiency`.

Verify missing/empty-field behavior across all Soccer Lab projectors:

```bash
./tools/data/verify_projector_missing_fields.py
```

Verify malformed-line and malformed-frame behavior across Soccer Lab projectors:

```bash
./tools/data/verify_projector_malformed_input.py
```

When a projector fails, it exits nonzero, writes no vector frame to stdout, and
emits one JSON object to stderr. The stable stderr schema is:

```json
{"event":"soccer_lab_projector_error","schema_version":1,"facet":"attack","input_hash":"<sha256>","reason":"malformed_token"}
```

`facet` is the endpoint executable basename, `input_hash` is the SHA-256 of the
failing input bytes (or the malformed frame/item bytes), and `reason` is the
fail-closed projector reason such as `malformed_token`, `invalid_number`,
`invalid_boolean`, `invalid_utf8`, `invalid_json_frame`, or
`input_not_byte_array`. During `calyx ingest`, the external-cmd runtime preserves
the projector object in the `stderr_tail=` portion of the `CALYX_LENS_UNREACHABLE`
message and then appends Calyx's own structured engine error. Read the projector
object first for the facet-local cause, then the Calyx object for the engine
error code.

Verify the projector structured-error stderr contract and Calyx propagation:

```bash
./tools/data/verify_projector_structured_errors.py
```

Verify non-finite numeric inputs and external-cmd non-finite vector rejection:

```bash
./tools/data/verify_projector_numerical_invariant.py
```

Verify wrong-length external-cmd vector rejection:

```bash
./tools/data/verify_projector_dim_mismatch.py
```

Verify Soccer Lab team/match A7 signal and decorrelation thresholds:

```bash
./tools/data/verify_soccer_lab_a7_audit.py
```

Verify ex-ante predictive-panel partitioning and ex-post anchor separation:

```bash
./tools/data/verify_soccer_lab_ex_ante_partition.py
```

The machine-readable policy is
`docs/data/soccer_lab_predictive_partition.json`. It defines which facets may be
registered in predictive panels, which anchor axes carry the ex-post outcomes,
and which current-event keys are forbidden in generated predictive `text`.

Verify Soccer Lab Oracle occurrence metadata on generated outcome rows:

```bash
./tools/data/verify_soccer_lab_oracle_metadata.py
```

This regenerates the outcome-bearing Soccer Lab rows, verifies `oracle.domain`,
`oracle.action`, and outcome anchors are present where real outcomes exist,
ingests a sampled real batch into a fresh vault, reads Base bytes plus Recurrence
contexts back from Calyx, and exercises fail-closed malformed Oracle edge cases.

Build and verify the teams-history ex-ante Calyx vault from the Harrachi
team-tournament dataset:

```bash
./tools/data/verify_soccer_lab_teams_history_vault.py
```

This downloads the public Kaggle zip if it is missing from
`scratchpad/wc2026/raw/harrachimustapha/`, generates 240 team-tournament rows,
creates a fresh vault, registers the seven team/match facet projectors, ingests
the rows, and verifies `cx-list --include-slots` has every expected dense slot.

Build and verify the 2026 matches ex-ante Calyx vault from the swaptr match
dataset enriched with Harrachi pre-tournament team priors:

```bash
./tools/data/verify_soccer_lab_matches_vault.py
```

This downloads the public swaptr and Harrachi Kaggle zips if missing, generates
85 match rows without post-match stat leakage in `text`, creates a fresh vault,
registers the seven team/match facet projectors, ingests the rows, and verifies
physical `cx-list --include-slots` slot presence plus vault bytes.

Build and verify the 2026 players ex-ante Calyx vault from the Mominullptr squad
dataset:

```bash
./tools/data/verify_soccer_lab_players_vault.py
```

This downloads the public Mominullptr Kaggle zip if missing, generates 1,248
player rows without post-match player-stat leakage in `text`, creates a fresh
vault, registers the three player facet projectors, ingests the rows, and
verifies physical `cx-list --include-slots` slot presence plus vault bytes.
