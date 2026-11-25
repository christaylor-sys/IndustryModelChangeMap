"""Read the raw export, filter to one geography level, reshape for analysis."""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from . import schema as s

log = logging.getLogger(__name__)

_BLANKS = {"", "NULL", "NA", "NAN", "N/A", "NONE"}


class SchemaError(ValueError):
    """The input file is missing columns or has no rows at the requested level."""


def read_raw(path: str | Path, v: s.Versions) -> pd.DataFrame:
    """Read the CSV with every identifier kept as text.

    Codes are read as strings so FIPS '01125' keeps its leading zero and
    MODELCODE / LOBCODE can hold 'ALL'. Only the columns the pipeline uses
    are loaded.
    """
    header = pd.read_csv(path, nrows=0, encoding="utf-8-sig").columns
    by_clean = {c.strip().upper(): c for c in header}

    numeric = s.value_cols(v.old) + s.value_cols(v.new)
    required = s.ID_COLS + numeric
    missing = [c for c in required if c not in by_clean]
    if missing:
        raise SchemaError(
            f"{len(missing)} required column(s) missing from {Path(path).name}: "
            + ", ".join(missing[:10]) + (" ..." if len(missing) > 10 else "")
            + f"\nCheck the version prefixes (expected {v.old}_ / {v.new}_)."
        )
    optional = [c for c in s.diff_cols() if c in by_clean]
    wanted = required + optional

    df = pd.read_csv(
        path,
        usecols=[by_clean[c] for c in wanted],
        dtype=str,
        keep_default_na=False,  # 'NA' is a valid code (e.g. Namibia), not a null
        encoding="utf-8-sig",
    )
    df.columns = [c.strip().upper() for c in df.columns]
    df.insert(0, "row_number", np.arange(len(df)) + 2)  # line number in the CSV

    for c in s.ID_COLS:
        df[c] = df[c].str.strip()

    bad_cells = 0
    for c in numeric + optional:
        raw = df[c].str.strip().str.replace(",", "", regex=False)
        raw = raw.where(~raw.str.upper().isin(_BLANKS), "")
        num = pd.to_numeric(raw, errors="coerce")
        bad_cells += int((num.isna() & raw.ne("")).sum())
        df[c] = num.astype("float64")
    if bad_cells:
        log.warning("%d non-numeric value cell(s) set to blank; see 'missing_value' "
                    "in the validation report", bad_cells)

    for c in ("LATITUDE", "LONGITUDE"):
        df[c] = pd.to_numeric(df[c], errors="coerce")

    log.info("Read %s rows from %s", f"{len(df):,}", Path(path).name)
    return df


def select_level(df: pd.DataFrame, level: str = "SUBA") -> pd.DataFrame:
    """Keep one geography level (SUBA = county)."""
    out = df[df["GEOLEVELCODE"] == level].copy()
    if out.empty:
        levels = ", ".join(sorted(df["GEOLEVELCODE"].unique()))
        raise SchemaError(f"No rows with GEOLEVELCODE={level}. Levels present: {levels}")
    dropped = len(df) - len(out)
    if dropped:
        counts = df.loc[df["GEOLEVELCODE"] != level, "GEOLEVELCODE"].value_counts()
        log.info("Kept %s %s rows; set aside %s",
                 f"{len(out):,}", level, dict(counts))
    return out.reset_index(drop=True)


def normalise_fips(df: pd.DataFrame) -> pd.DataFrame:
    """Restore leading zeros lost when a file passes through Excel (1125 -> 01125)."""
    fips = df[s.FIPS]
    short = fips.str.fullmatch(r"\d{1,4}")
    if short.any():
        log.warning("%s FIPS code(s) had lost leading zeros (file probably saved "
                    "via Excel); padded to 5 digits", f"{int(short.sum()):,}")
        df[s.FIPS] = fips.where(~short, fips.str.zfill(5))
    return df


def to_long(df: pd.DataFrame, v: s.Versions) -> pd.DataFrame:
    """One row per county x model x peril x LOB x metric x return period.

    Columns: fips, model_code, peril, lob_code, curve (AAL/OEP/AEP),
    return_period (blank for AAL), <old>, <new>. The DIFF / PCT columns are
    dropped: they are recomputed downstream so they can never disagree.
    """
    keys = df[s.KEY].rename(columns={
        s.FIPS: "fips", "MODELCODE": "model_code",
        "PERIL": "peril", "LOBCODE": "lob_code",
    })
    old, new = v.old.lower(), v.new.lower()
    pieces = []
    for suffix in s.metric_suffixes():
        if suffix == "AAL":
            curve, rp = "AAL", np.nan
        else:
            curve, rp_txt = suffix.split("_")
            rp = float(rp_txt)
        piece = keys.copy()
        piece["curve"] = curve
        piece["return_period"] = rp
        piece[old] = df[f"{v.old}_{suffix}"].to_numpy()
        piece[new] = df[f"{v.new}_{suffix}"].to_numpy()
        pieces.append(piece)

    long = pd.concat(pieces, ignore_index=True)
    long["return_period"] = long["return_period"].astype("Int16")
    for c in ("fips", "model_code", "peril", "lob_code", "curve"):
        long[c] = long[c].astype("category")
    return long


def geography_dim(df: pd.DataFrame) -> pd.DataFrame:
    """One row per county: names and centroid for labels and popups."""
    cols = {
        s.FIPS: "fips", "SUBAREANAME": "county", "AREACODE": "state_code",
        "AREANAME": "state", "LATITUDE": "lat", "LONGITUDE": "lon",
        "GEOGRAPHYSID": "geography_sid",
    }
    return (df[list(cols)].rename(columns=cols)
            .drop_duplicates("fips").sort_values("fips").reset_index(drop=True))


def slice_dim(df: pd.DataFrame) -> pd.DataFrame:
    """Distinct model / peril / LOB combinations with their labels."""
    cols = {
        "MODELCODE": "model_code", "MODEL_NAME": "model_name", "PERIL": "peril",
        "LOBCODE": "lob_code", "LOBDESCRIPTION": "lob_description",
    }
    counts = df.groupby(list(cols), sort=False).size().rename("rows")
    return (counts.reset_index().rename(columns=cols)
            .sort_values(["peril", "model_code", "lob_code"]).reset_index(drop=True))
