"""Ingest the model-change EP export, validate it, and write map-ready outputs.

Example:
    python run_pipeline.py data/raw/export.csv
    python run_pipeline.py data/raw/export.csv --old V13 --new V14 --allow-errors
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from pipeline import export, ingest, validate
from pipeline.schema import Versions

log = logging.getLogger("pipeline")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", type=Path, help="raw CSV export")
    p.add_argument("--processed-dir", type=Path, default=Path("data/processed"),
                   help="Parquet outputs and validation report (default: data/processed)")
    p.add_argument("--web-dir", type=Path, default=Path("web/data"),
                   help="JSON files for the map (default: web/data)")
    p.add_argument("--old", default="V12", help="baseline version prefix (default: V12)")
    p.add_argument("--new", default="V13", help="new version prefix (default: V13)")
    p.add_argument("--level", default="SUBA", help="GEOLEVELCODE to keep (default: SUBA = county)")
    p.add_argument("--allow-errors", action="store_true",
                   help="write outputs even if validation finds errors")
    p.add_argument("--no-gzip", action="store_true",
                   help="write plain .json slices (if your web host compresses for you)")
    p.add_argument("--skip-web", action="store_true", help="only write Parquet outputs")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s")
    start = time.perf_counter()
    v = Versions(args.old.upper(), args.new.upper())

    try:
        raw = ingest.read_raw(args.input, v)
        df = ingest.select_level(raw, args.level)
    except (ingest.SchemaError, FileNotFoundError) as exc:
        log.error("%s", exc)
        return 2
    ingest.normalise_fips(df)

    issues = validate.run_all(df, v)
    args.processed_dir.mkdir(parents=True, exist_ok=True)
    report = args.processed_dir / "validation_report.csv"
    issues.to_csv(report, index=False)
    print("\n" + validate.summary(issues, df) + f"\n\nFull report: {report}\n")

    n_errors = int((issues["severity"] == "error").sum())
    if n_errors and not args.allow_errors:
        log.error("%s row(s) failed error-level checks; no outputs written. "
                  "Fix the source or rerun with --allow-errors.", f"{n_errors:,}")
        return 1

    geo, slices = ingest.geography_dim(df), ingest.slice_dim(df)
    export.write_parquet(ingest.to_long(df, v), geo, slices, args.processed_dir, v)
    if not args.skip_web:
        export.write_web(df, geo, slices, args.web_dir, v, source=args.input.name,
                         compress=not args.no_gzip)

    log.info("Done in %.1fs", time.perf_counter() - start)
    return 0


if __name__ == "__main__":
    sys.exit(main())
