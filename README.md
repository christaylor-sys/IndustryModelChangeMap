# EP map: ingest pipeline

Turns the V12 vs V13 county-level EP export into validated, map-ready data.

```bash
pip install -e .            # pandas, pyarrow (pytest for tests)
python run_pipeline.py data/raw/<export>.csv
python -m pytest -q
```

Put the raw CSV in `data/raw/` (gitignored). A 250k-row file takes about 20 seconds.

## What it does

1. **Read** only the ~50 columns it needs. Every code is read as text, so FIPS `01125` keeps its zero and `ALL` survives in `MODELCODE` / `LOBCODE`. Codes that Excel shortened (`1125`) are padded back.
2. **Filter** to `GEOLEVELCODE = SUBA` (counties). Use `--level` for another level.
3. **Validate** (below). It stops on errors unless you pass `--allow-errors`.
4. **Write**
   - `data/processed/ep_long.parquet`: one row per county × model × peril × LOB × metric × return period, with columns `v12` and `v13`. DIFF and PCT are dropped, because they're recomputed wherever they're needed.
   - `data/processed/dim_geography.parquet` and `dim_slices.parquet`: county names and coordinates, and model/peril/LOB labels.
   - `data/processed/validation_report.csv`: every flagged row with its CSV line number.
   - `web/data/`: `manifest.json`, `geography.json` and one file per peril × LOB in `slices/`. Each slice file holds about 3,100 counties × 34 numbers, in the order given by `manifest.layout`.

## Checks

| Check | Severity | Meaning |
|---|---|---|
| `fips_format` | error | Code isn't 5 digits |
| `duplicate_key` | error | County / model / peril / LOB repeated |
| `negative_value` | error | Loss below zero |
| `ep_not_monotonic` | error | Loss falls as return period rises |
| `aep_below_oep` | error | Aggregate below occurrence at the same RP |
| `missing_value` | warning | Blank or non-numeric value |
| `fips_conflict` | warning | One FIPS has several names or GEOGRAPHYSIDs |
| `legacy_fips` | warning | Code missing from current boundaries (Connecticut counties, Valdez-Cordova, etc.) |
| `diff_mismatch`, `pct_diff_mismatch` | warning | Export's derived columns disagree with V13 − V12 |
| `aal_rollup_lob`, `aal_rollup_peril` | warning | AAL of the `ALL` row ≠ sum of its components (±1%). AAL is additive. |
| `ep_rollup_lob`, `ep_rollup_peril` | warning | EP of the `ALL` row is below one of its components. EP isn't additive, but the total can't be smaller than any part. |
| `pct_change_undefined` | info | V12 AAL is 0 but V13 isn't |

The AAL rollup warning can be legitimate if the export leaves out some LOBs or perils that are in the `ALL` total.

## Options

```
--old V12 --new V13     version prefixes (for the next model update)
--processed-dir / --web-dir
--allow-errors          write outputs anyway (duplicates: first row kept)
--no-gzip               plain .json slices, if your host compresses for you
--skip-web              Parquet only
```

The slices are gzipped by default. The web page decompresses them with `DecompressionStream('gzip')`. If your host serves them with `Content-Encoding: gzip`, the browser already decompresses them, so use `--no-gzip`.
