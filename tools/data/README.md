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
