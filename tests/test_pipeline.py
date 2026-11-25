from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pipeline import export, ingest, validate
from pipeline import schema as s

SAMPLE = Path(__file__).parent / "fixtures" / "sample.csv"
V = s.Versions()


@pytest.fixture
def df():
    return ingest.normalise_fips(ingest.select_level(ingest.read_raw(SAMPLE, V)))


def checks_found(frame):
    return set(validate.run_all(frame, V)["check"])


def test_sample_is_clean(df):
    assert validate.run_all(df, V).empty


def test_codes_stay_text(df):
    assert df.loc[0, s.FIPS] == "01125"
    assert set(df["MODELCODE"]) >= {"ALL", "22"}


def test_long_shape(df):
    long = ingest.to_long(df, V)
    assert len(long) == len(df) * 17
    aal = long[long.curve == "AAL"]
    assert aal["return_period"].isna().all()
    row = long[(long.fips == "01125") & (long.curve == "OEP") & (long.return_period == 100)]
    assert row["v13"].item() == 10_140_000


def test_lost_leading_zeros_restored(tmp_path):
    text = SAMPLE.read_text().replace(",01125,", ",1125,")
    path = tmp_path / "excel.csv"
    path.write_text(text)
    frame = ingest.normalise_fips(ingest.select_level(ingest.read_raw(path, V)))
    assert "01125" in set(frame[s.FIPS])


def test_missing_column_raises(tmp_path):
    raw = pd.read_csv(SAMPLE, dtype=str, keep_default_na=False).drop(columns="V13_AEP_500")
    path = tmp_path / "short.csv"
    raw.to_csv(path, index=False)
    with pytest.raises(ingest.SchemaError, match="V13_AEP_500"):
        ingest.read_raw(path, V)


def test_wrong_level_raises():
    raw = ingest.read_raw(SAMPLE, V)
    with pytest.raises(ingest.SchemaError, match="SUBA"):
        ingest.select_level(raw, "AREA")


def test_non_monotonic(df):
    df.loc[0, "V13_OEP_250"] = df.loc[0, "V13_OEP_200"] - 1000
    issues = validate.run_all(df, V)
    hit = issues[issues.check == "ep_not_monotonic"]
    assert len(hit) == 1 and "200yr" in hit.detail.item()


def test_aep_below_oep(df):
    df.loc[1, "V12_AEP_100"] = df.loc[1, "V12_OEP_100"] / 2
    assert "aep_below_oep" in checks_found(df)


def test_duplicate_key(df):
    dup = pd.concat([df, df.iloc[[0]]], ignore_index=True)
    assert "duplicate_key" in checks_found(dup)


def test_negative_and_missing(df):
    df.loc[2, "V12_AAL"] = -5
    df.loc[3, "V13_OEP_10"] = np.nan
    found = checks_found(df)
    assert {"negative_value", "missing_value"} <= found


def test_diff_and_pct_mismatch(df):
    df.loc[0, "DIFF_OEP_100"] += 50_000
    df.loc[1, "PCT_DIFF_AAL"] = 5.0  # percentage entered instead of a decimal
    assert {"diff_mismatch", "pct_diff_mismatch"} <= checks_found(df)


def test_legacy_connecticut(df):
    df.loc[0, s.FIPS] = "09003"
    assert "legacy_fips" in checks_found(df)


def _rollup_frame(df, total_aal, total_oep_1000):
    """County 01125, NACS: two LOB rows plus an ALL row."""
    base = df.iloc[[0]].copy()
    a, b, t = base.copy(), base.copy(), base.copy()
    b["LOBCODE"] = "3"
    t["LOBCODE"] = "ALL"
    for ver in V.both:
        t[f"{ver}_AAL"] = total_aal
        t[f"{ver}_OEP_1000"] = total_oep_1000
        t[f"{ver}_AEP_1000"] = max(total_oep_1000, float(base[f"{ver}_AEP_1000"].iloc[0]))
    out = pd.concat([a, b, t], ignore_index=True)
    out["row_number"] = range(2, 5)
    return out


def test_rollup_consistent_passes(df):
    aal = df.loc[0, "V12_AAL"]
    frame = _rollup_frame(df, total_aal=2 * aal, total_oep_1000=40e6)
    frame.loc[2, "V13_AAL"] = 2 * df.loc[0, "V13_AAL"]
    found = checks_found(frame)
    assert not {c for c in found if c.startswith(("aal_rollup", "ep_rollup"))}


def test_rollup_mismatch_flagged(df):
    # total AAL far from the sum of parts, and total OEP below a single part
    frame = _rollup_frame(df, total_aal=1.0e6, total_oep_1000=1.0e6)
    found = checks_found(frame)
    assert {"aal_rollup_lob", "ep_rollup_lob"} <= found


def test_web_export(df, tmp_path):
    geo, slices = ingest.geography_dim(df), ingest.slice_dim(df)
    export.write_web(df, geo, slices, tmp_path, V, source="sample.csv", compress=False)
    import json
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert len(manifest["slices"]) == 5
    nacs = next(x for x in manifest["slices"] if x["peril"] == "NACS")
    data = json.loads((tmp_path / nacs["file"]).read_text())
    row = data["01125"]
    assert len(row) == len(manifest["layout"]) == 34
    assert row[manifest["layout"].index("V13_AEP_1000")] == 31_460_000
