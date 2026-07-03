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
./tools/data/generate_soccer_lab_rows.py --only players --only matches --only teams-history --only fjelstul
```

Outputs are written to `scratchpad/wc2026/rows/`:

- `players.jsonl`
- `matches.jsonl`
- `teams-history.jsonl`
- `fjelstul.jsonl`
- `fixtures.jsonl`

Match and team-history rows keep ex-post results out of `text`; outcomes are
stored only as grounded anchors.
