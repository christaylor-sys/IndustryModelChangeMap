"""Data-quality checks on the county-level wide table.

Each check returns rows for an issues table with columns:
check, severity, row_number, fips, model_code, peril, lob_code, detail

Severities:
  error   - the data is wrong or ambiguous; the pipeline stops unless --allow-errors
  warning - worth investigating, but the outputs are still usable
  info    - expected quirks worth knowing about
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import schema as s

ABS_TOL = 1.0     # dollars; the export is rounded to cents
REL_TOL = 1e-6
ROLLUP_TOL = 0.01  # 1% for AAL additivity

ISSUE_COLS = ["check", "severity", "row_number", "fips", "model_code",
              "peril", "lob_code", "detail"]


def _tol(x: np.ndarray) -> np.ndarray:
    return np.maximum(ABS_TOL, REL_TOL * np.abs(x))


def _fmt(x: float) -> str:
    if pd.isna(x):
        return "blank"
    for div, unit in ((1e9, "bn"), (1e6, "m"), (1e3, "k")):
        if abs(x) >= div:
            return f"{x / div:,.2f}{unit}"
    return f"{x:,.0f}"


def _issues(df: pd.DataFrame, mask, check: str, severity: str, detail) -> pd.DataFrame:
    mask = np.asarray(mask, dtype=bool)
    if not mask.any():
        return pd.DataFrame(columns=ISSUE_COLS)
    sub = df.loc[mask]
    if callable(detail):
        detail = [detail(i) for i in np.flatnonzero(mask)]
    return pd.DataFrame({
        "check": check, "severity": severity,
        "row_number": sub["row_number"].to_numpy(),
        "fips": sub[s.FIPS].to_numpy(),
        "model_code": sub["MODELCODE"].to_numpy(),
        "peril": sub["PERIL"].to_numpy(),
        "lob_code": sub["LOBCODE"].to_numpy(),
        "detail": detail,
    })


# --- identity and geography ---------------------------------------------------

def check_fips_format(df, v):
    bad = ~df[s.FIPS].str.fullmatch(r"\d{5}")
    return [_issues(df, bad, "fips_format", "error",
                    lambda i: f"'{df[s.FIPS].iat[i]}' is not a 5-digit FIPS code")]


def check_duplicate_keys(df, v):
    dup = df.duplicated(s.KEY, keep=False)
    return [_issues(df, dup, "duplicate_key", "error",
                    "same county / model / peril / LOB appears more than once")]


def check_fips_conflicts(df, v):
    per = df.groupby(s.FIPS)[["GEOGRAPHYSID", "SUBAREANAME"]].nunique()
    conflicted = per[(per > 1).any(axis=1)].index
    mask = df[s.FIPS].isin(conflicted)
    return [_issues(df, mask, "fips_conflict", "warning",
                    "this FIPS maps to more than one GEOGRAPHYSID or county name")]


def check_legacy_fips(df, v):
    mask = df[s.FIPS].isin(s.LEGACY_FIPS.keys())
    return [_issues(df, mask, "legacy_fips", "warning",
                    lambda i: s.LEGACY_FIPS[df[s.FIPS].iat[i]]
                    + "; will not join to current county boundaries")]


# --- values ---------------------------------------------------------------------

def check_missing_and_negative(df, v):
    cols = s.value_cols(v.old) + s.value_cols(v.new)
    vals = df[cols].to_numpy()
    nan = np.isnan(vals)
    neg = vals < -ABS_TOL
    names = np.array(cols)
    return [
        _issues(df, nan.any(axis=1), "missing_value", "warning",
                lambda i: f"{nan[i].sum()} blank value(s): "
                          + ", ".join(names[nan[i]][:4]) + (" ..." if nan[i].sum() > 4 else "")),
        _issues(df, neg.any(axis=1), "negative_value", "error",
                lambda i: "negative loss in " + ", ".join(names[neg[i]][:4])),
    ]


def check_monotonic(df, v):
    """Loss must not fall as return period increases."""
    out = []
    rps = s.RETURN_PERIODS
    for ver in v.both:
        for curve in s.CURVES:
            vals = df[s.ep_cols(ver, curve)].to_numpy()
            drops = np.diff(vals, axis=1) < -_tol(vals[:, :-1])
            bad = drops.any(axis=1)

            def detail(i, vals=vals, drops=drops, ver=ver, curve=curve):
                j = int(np.argmax(drops[i]))
                return (f"{ver} {curve} falls from {_fmt(vals[i, j])} at {rps[j]}yr "
                        f"to {_fmt(vals[i, j + 1])} at {rps[j + 1]}yr")
            out.append(_issues(df, bad, "ep_not_monotonic", "error", detail))
    return out


def check_aep_ge_oep(df, v):
    """Annual aggregate loss is at least the largest single event at every RP."""
    out = []
    rps = s.RETURN_PERIODS
    for ver in v.both:
        oep = df[s.ep_cols(ver, "OEP")].to_numpy()
        aep = df[s.ep_cols(ver, "AEP")].to_numpy()
        below = aep < oep - _tol(oep)

        def detail(i, oep=oep, aep=aep, below=below, ver=ver):
            j = int(np.argmax(below[i]))
            return (f"{ver} AEP {_fmt(aep[i, j])} < OEP {_fmt(oep[i, j])} "
                    f"at {rps[j]}yr")
        out.append(_issues(df, below.any(axis=1), "aep_below_oep", "error", detail))
    return out


def check_derived_columns(df, v):
    """DIFF_* and PCT_DIFF_AAL should match V_new - V_old. Only if present."""
    out = []
    diffs = [c for c in s.diff_cols() if c in df.columns and c != "PCT_DIFF_AAL"]
    if diffs:
        suffixes = [c[len("DIFF_"):] for c in diffs]
        given = df[diffs].to_numpy()
        calc = (df[[f"{v.new}_{x}" for x in suffixes]].to_numpy()
                - df[[f"{v.old}_{x}" for x in suffixes]].to_numpy())
        bad = np.abs(given - calc) > _tol(calc)
        bad &= ~np.isnan(given) & ~np.isnan(calc)
        names = np.array(diffs)
        out.append(_issues(df, bad.any(axis=1), "diff_mismatch", "warning",
                           lambda i: f"DIFF != {v.new} - {v.old} in "
                                     + ", ".join(names[bad[i]][:4])))

    old, new = df[f"{v.old}_AAL"].to_numpy(), df[f"{v.new}_AAL"].to_numpy()
    zero_old = (np.abs(old) <= ABS_TOL) & (np.abs(new) > ABS_TOL)
    out.append(_issues(df, zero_old, "pct_change_undefined", "info",
                       lambda i: f"{v.old} AAL is 0 but {v.new} AAL is {_fmt(new[i])}; "
                                 "% change will show as 'new'"))
    if "PCT_DIFF_AAL" in df.columns:
        pct = df["PCT_DIFF_AAL"].to_numpy()
        with np.errstate(divide="ignore", invalid="ignore"):
            calc = new / old - 1
        bad = ~zero_old & (np.abs(old) > ABS_TOL) & ~np.isnan(pct) & (np.abs(pct - calc) > 5e-4)
        out.append(_issues(df, bad, "pct_diff_mismatch", "warning",
                           lambda i: f"PCT_DIFF_AAL is {pct[i]:.4f}, expected {calc[i]:.4f} "
                                     "(should be a decimal, 0.05 = 5%)"))
    return out


# --- rollups ----------------------------------------------------------------------

def _rollup(df, v, part_col, group_cols, label):
    """Compare an ALL row with its components.

    AAL is an expectation, so it adds: components should sum to the total.
    EP losses do not add, but the total must be at least as large as every
    component at each return period.
    """
    clean = df.drop_duplicates(s.KEY)
    is_total = clean[part_col] == s.ALL
    parts = clean[~is_total]
    if parts.empty or not is_total.any():
        return []
    g = parts.groupby(group_cols, sort=False)
    totals = clean[is_total].set_index(group_cols)
    common = totals.index.intersection(g.size().index)
    if common.empty:
        return []
    totals = totals.loc[common]
    flat = totals.reset_index()
    out = []
    for ver in v.both:
        aal = f"{ver}_AAL"
        tot = totals[aal].to_numpy()
        summed = g[aal].sum().loc[common].to_numpy()
        n = g.size().loc[common].to_numpy()
        bad = np.abs(summed - tot) > np.maximum(ABS_TOL, ROLLUP_TOL * np.abs(tot))
        out.append(_issues(flat, bad, f"aal_rollup_{label}", "warning",
                           lambda i, tot=tot, summed=summed, n=n, ver=ver:
                           f"{ver}: {n[i]} {label} component(s) sum to {_fmt(summed[i])} "
                           f"vs ALL row {_fmt(tot[i])} ({summed[i] / tot[i] - 1:+.1%})"
                           if tot[i] else f"{ver}: components sum to {_fmt(summed[i])} vs ALL row 0"))
        for curve in s.CURVES:
            cols = s.ep_cols(ver, curve)
            t = totals[cols].to_numpy()
            mx = g[cols].max().loc[common].to_numpy()
            below = t < mx - _tol(mx)

            def detail(i, t=t, mx=mx, below=below, ver=ver, curve=curve):
                j = int(np.argmax(below[i]))
                return (f"{ver} {curve} ALL row {_fmt(t[i, j])} is below its largest "
                        f"{label} component {_fmt(mx[i, j])} at {s.RETURN_PERIODS[j]}yr")
            out.append(_issues(flat, below.any(axis=1), f"ep_rollup_{label}", "warning", detail))
    return out


def check_rollups(df, v):
    return (_rollup(df, v, "LOBCODE", [s.FIPS, "MODELCODE", "PERIL"], "lob")
            + _rollup(df, v, "PERIL", [s.FIPS, "LOBCODE"], "peril"))


CHECKS = [
    check_fips_format, check_duplicate_keys, check_fips_conflicts, check_legacy_fips,
    check_missing_and_negative, check_monotonic, check_aep_ge_oep,
    check_derived_columns, check_rollups,
]


def run_all(df: pd.DataFrame, v: s.Versions) -> pd.DataFrame:
    frames = [f for check in CHECKS for f in check(df, v) if not f.empty]
    if not frames:
        return pd.DataFrame(columns=ISSUE_COLS)
    order = {"error": 0, "warning": 1, "info": 2}
    issues = pd.concat(frames, ignore_index=True)
    return (issues.sort_values(["severity", "check", "row_number"],
                               key=lambda c: c.map(order) if c.name == "severity" else c)
            .reset_index(drop=True))


def summary(issues: pd.DataFrame, df: pd.DataFrame, examples: int = 3) -> str:
    lines = [
        f"Counties: {df[s.FIPS].nunique():,}   Rows: {len(df):,}   "
        f"Perils: {', '.join(sorted(df['PERIL'].unique()))}   "
        f"LOBs: {', '.join(sorted(df['LOBCODE'].unique()))}",
    ]
    if issues.empty:
        lines.append("All checks passed.")
        return "\n".join(lines)
    counts = issues.groupby(["severity", "check"], sort=False).size()
    lines.append("")
    for (sev, check), n in counts.items():
        lines.append(f"  {sev.upper():8} {check:24} {n:>8,} row(s)")
        for _, r in issues[issues["check"] == check].head(examples).iterrows():
            lines.append(f"           line {r.row_number}  {r.fips} {r.peril}/{r.lob_code}: {r.detail}")
    return "\n".join(lines)
