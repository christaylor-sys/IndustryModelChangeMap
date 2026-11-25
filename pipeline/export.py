"""Write the outputs: Parquet for analysis, small JSON files for the web map.

Web layout (web/data/):
  manifest.json         versions, return periods, value layout, list of slices
  geography.json        fips -> [county, state code]
  slices/<model>_<peril>_<lob>.json[.gz]
                        {"fips": [v12_AAL, v13_AAL, v12_OEP_10 ... v13_AEP_1000]}

The map loads one slice (one peril x LOB) at a time, so each file is about
3,100 counties x 34 numbers: roughly 1 MB, or 0.3 MB gzipped.
"""
from __future__ import annotations

import gzip
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import schema as s

log = logging.getLogger(__name__)


def web_layout(v: s.Versions) -> list[str]:
    """Order of the 34 numbers stored per county in every slice file."""
    layout = [f"{v.old}_AAL", f"{v.new}_AAL"]
    for curve in s.CURVES:
        for ver in v.both:
            layout += s.ep_cols(ver, curve)
    return layout


def _slug(*parts: str) -> str:
    return "_".join(re.sub(r"[^A-Za-z0-9]+", "-", p).strip("-") or "x" for p in parts)


def _dump(obj, path: Path, compress: bool) -> int:
    data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
    if compress:
        path = path.with_name(path.name + ".gz")
        with open(path, "wb") as fh, gzip.GzipFile(fileobj=fh, mode="wb", mtime=0) as gz:
            gz.write(data)
    else:
        path.write_bytes(data)
    return path.stat().st_size


def write_parquet(long: pd.DataFrame, geo: pd.DataFrame, slices: pd.DataFrame,
                  out_dir: Path, v: s.Versions) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    long.sort_values(["peril", "lob_code", "fips", "curve", "return_period"]).to_parquet(
        out_dir / "ep_long.parquet", index=False, compression="zstd")
    geo.to_parquet(out_dir / "dim_geography.parquet", index=False)
    slices.to_parquet(out_dir / "dim_slices.parquet", index=False)
    log.info("Wrote %s long rows to %s", f"{len(long):,}", out_dir / "ep_long.parquet")


def write_web(df: pd.DataFrame, geo: pd.DataFrame, slices: pd.DataFrame,
              out_dir: Path, v: s.Versions, source: str,
              compress: bool = True, decimals: int = 0) -> None:
    slice_dir = out_dir / "slices"
    slice_dir.mkdir(parents=True, exist_ok=True)
    for old in slice_dir.glob("*.json*"):
        old.unlink()

    layout = web_layout(v)
    df = df.drop_duplicates(s.KEY)  # duplicates are reported by validation
    manifest_slices, total_bytes = [], 0

    for (model, peril, lob), grp in df.groupby(["MODELCODE", "PERIL", "LOBCODE"], sort=True):
        vals = np.round(grp[layout].to_numpy(dtype="float64"), decimals)
        nan = np.isnan(vals)
        if decimals == 0:
            vals = np.where(nan, 0, vals).astype(np.int64)
        cells = np.where(nan, None, vals.astype(object)).tolist()
        payload = dict(zip(grp[s.FIPS].tolist(), cells))

        name = _slug(model, peril, lob) + ".json"
        size = _dump(payload, slice_dir / name, compress)
        total_bytes += size
        label = slices[(slices.model_code == model) & (slices.peril == peril)
                       & (slices.lob_code == lob)].iloc[0]
        manifest_slices.append({
            "model_code": model, "model_name": label.model_name, "peril": peril,
            "lob_code": lob, "lob_description": label.lob_description,
            "file": f"slices/{name}" + (".gz" if compress else ""),
            "counties": len(payload),
        })

    geo_payload = {r.fips: [r.county, r.state_code] for r in geo.itertuples()}
    _dump(geo_payload, out_dir / "geography.json", compress=False)

    manifest = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "versions": {"old": v.old, "new": v.new},
        "return_periods": list(s.RETURN_PERIODS),
        "layout": layout,
        "gzip": compress,
        "all_code": s.ALL,
        "slices": manifest_slices,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    log.info("Wrote %d slice files (%.1f MB) to %s",
             len(manifest_slices), total_bytes / 1e6, slice_dir)
