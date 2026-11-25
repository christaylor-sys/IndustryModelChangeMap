"""Column names and constants for the model-change EP export.

All column names are built here, so a later model update (e.g. V13 -> V14)
or a change in return periods is a one-line edit.
"""
from __future__ import annotations

from dataclasses import dataclass

RETURN_PERIODS: tuple[int, ...] = (10, 25, 50, 100, 200, 250, 500, 1000)
CURVES: tuple[str, ...] = ("OEP", "AEP")
ALL = "ALL"  # rollup marker used in MODELCODE / PERIL / LOBCODE

# Identity columns: one row per county x model x peril x LOB.
FIPS = "SUBAREACODE"
KEY = [FIPS, "MODELCODE", "PERIL", "LOBCODE"]

# Non-metric columns the pipeline uses. The other ~20 reference columns in
# the export (CRESTA, postal, GUIDs, parent SIDs ...) are not read at all.
ID_COLS = [
    "GEOGRAPHYSID", "GEOLEVELCODE", "AREACODE", "AREANAME", FIPS,
    "SUBAREANAME", "LATITUDE", "LONGITUDE", "MODELCODE", "MODEL_NAME",
    "PERIL", "LOBCODE", "LOBDESCRIPTION",
]


@dataclass(frozen=True)
class Versions:
    old: str = "V12"
    new: str = "V13"

    @property
    def both(self) -> tuple[str, str]:
        return (self.old, self.new)


def metric_suffixes() -> list[str]:
    """AAL, OEP_10 ... OEP_1000, AEP_10 ... AEP_1000 (17 metrics)."""
    return ["AAL"] + [f"{c}_{rp}" for c in CURVES for rp in RETURN_PERIODS]


def value_cols(version: str) -> list[str]:
    return [f"{version}_{s}" for s in metric_suffixes()]


def ep_cols(version: str, curve: str) -> list[str]:
    return [f"{version}_{curve}_{rp}" for rp in RETURN_PERIODS]


def diff_cols() -> list[str]:
    """Derived columns in the export. Optional: only used for cross-checks."""
    return [f"DIFF_{s}" for s in metric_suffixes()] + ["PCT_DIFF_AAL"]


# FIPS codes that no longer exist in current Census / Esri Living Atlas
# county boundaries, so they will not join to a polygon.
LEGACY_FIPS = {
    **{f"090{n:02d}": "Connecticut county replaced by planning regions (09110-09190) in 2022"
       for n in (1, 3, 5, 7, 9, 11, 13, 15)},
    "02261": "Valdez-Cordova (AK) split into 02063 / 02066 in 2019",
    "46113": "Shannon County (SD) renamed Oglala Lakota, now 46102",
    "51515": "Bedford City (VA) merged into Bedford County 51019 in 2013",
}
