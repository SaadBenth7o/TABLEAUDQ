"""
consolidate.py
──────────────
Standalone script to build consolidated DQ reporting files.

Scans today's (or all historical) output runs, filters out ERROR rows,
deduplicates by (dremio_col, virt_full_path, date), and appends only
new non-error results to the consolidated CSV and YAML files.

Usage
─────
  # Consolidate today's runs only
  python lib/consolidate.py

  # Consolidate ALL historical runs (first-time migration)
  python lib/consolidate.py --all

Output
──────
  output/
  └── consolidated/
      ├── all_columns_history.csv
      └── all_columns_history.yaml
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from lib.dremio_client import DremioClient

load_dotenv(ROOT_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

OUTPUT_DIR = ROOT_DIR / "output"
CONSOLIDATED_DIR = OUTPUT_DIR / "consolidated"

CONSOLIDATED_CSV = CONSOLIDATED_DIR / "all_columns_history.csv"
CONSOLIDATED_YAML = CONSOLIDATED_DIR / "all_columns_history.yaml"

DREMIO_PUBLISH_HOME_NAME = os.getenv("DREMIO_PUBLISH_HOME_NAME", "")
DREMIO_PUBLISH_FOLDER_PATH = os.getenv("DREMIO_PUBLISH_FOLDER_PATH", "")
DREMIO_PUBLISH_DATASET_NAME = os.getenv("DREMIO_PUBLISH_DATASET_NAME", "")
DREMIO_PUBLISH_AUTO = os.getenv("DREMIO_PUBLISH_AUTO", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

FIELDNAMES = [
    "dremio_col",
    "virt_full_path",
    "dataset",
    "domain",
    "rule",
    "total_lignes",
    "valides",
    "score_pct",
    "flag",
    "timestamp",
]


def _date_from_folder_name(folder_name: str) -> str | None:
    try:
        return folder_name[:10]
    except (IndexError, ValueError):
        return None


def _dedup_key(row: dict) -> tuple[str, str, str]:
    ts = row.get("timestamp", "")
    date_part = ts[:10] if len(ts) >= 10 else ts
    dremio_col = row.get("dremio_col", "") or row.get("\ufeffdremio_col", "")
    return (dremio_col, row.get("virt_full_path", ""), date_part)


def _load_existing_consolidated() -> tuple[list[dict], set[tuple]]:
    rows: list[dict] = []
    keys: set[tuple] = set()

    if not CONSOLIDATED_CSV.exists():
        return rows, keys

    with open(CONSOLIDATED_CSV, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("dremio_col") and row.get("\ufeffdremio_col"):
                row["dremio_col"] = row.get("\ufeffdremio_col", "")
            rows.append(row)
            keys.add(_dedup_key(row))

    logger.info("Loaded %d existing consolidated rows (%d unique keys)", len(rows), len(keys))
    return rows, keys


def _find_run_folders(target_dates: list[str] | None = None) -> list[Path]:
    if not OUTPUT_DIR.exists():
        return []

    folders = []
    for item in sorted(os.listdir(OUTPUT_DIR)):
        folder_path = OUTPUT_DIR / item
        if not folder_path.is_dir():
            continue
        if item == "consolidated":
            continue

        if target_dates is not None:
            folder_date = _date_from_folder_name(item)
            if folder_date not in target_dates:
                continue

        folders.append(folder_path)

    return folders


def _read_run_columns(run_folder: Path) -> list[dict]:
    csv_dir_name = f"{run_folder.name}_csv"
    csv_dir = run_folder / csv_dir_name

    if not csv_dir.exists():
        logger.warning("  No CSV subfolder found: %s", csv_dir)
        return []

    rows: list[dict] = []

    for csv_file in sorted(csv_dir.iterdir()):
        if not csv_file.name.endswith(".csv"):
            continue
        if csv_file.name == "_all_tables.csv":
            continue

        try:
            with open(csv_file, "r", newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("flag", "").upper() == "ERROR":
                        continue

                    if not row.get("dremio_col") and row.get("\ufeffdremio_col"):
                        row["dremio_col"] = row.get("\ufeffdremio_col", "")

                    clean_row = {field: row.get(field, "") for field in FIELDNAMES}
                    rows.append(clean_row)
        except Exception as exc:
            logger.error("  Error reading %s: %s", csv_file, exc)

    return rows


def _write_consolidated_csv(rows: list[dict]) -> None:
    CONSOLIDATED_DIR.mkdir(parents=True, exist_ok=True)

    with open(CONSOLIDATED_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    logger.info("Consolidated CSV written: %s (%d rows)", CONSOLIDATED_CSV, len(rows))


def _write_consolidated_yaml(rows: list[dict]) -> None:
    CONSOLIDATED_DIR.mkdir(parents=True, exist_ok=True)

    typed_rows = []
    for row in rows:
        typed = dict(row)
        for int_field in ("total_lignes", "valides"):
            if typed[int_field] and typed[int_field] != "N/A":
                try:
                    typed[int_field] = int(typed[int_field])
                except (ValueError, TypeError):
                    pass
        if typed["score_pct"] and typed["score_pct"] != "N/A":
            try:
                typed["score_pct"] = float(typed["score_pct"])
            except (ValueError, TypeError):
                pass
        typed_rows.append(typed)

    data = {"columns": typed_rows}

    with open(CONSOLIDATED_YAML, "w", encoding="utf-8") as fh:
        yaml.dump(data, fh, allow_unicode=True, sort_keys=False, default_flow_style=False)

    logger.info("Consolidated YAML written: %s (%d rows)", CONSOLIDATED_YAML, len(rows))


def consolidate(all_dates: bool = False) -> None:
    if all_dates:
        target_dates = None
        logger.info("Mode: --all (processing all historical runs)")
    else:
        today = datetime.now().strftime("%Y-%m-%d")
        target_dates = [today]
        logger.info("Mode: today only (%s)", today)

    run_folders = _find_run_folders(target_dates)
    if not run_folders:
        logger.warning("No run folders found for the target date(s).")
        return

    logger.info("Found %d run folder(s) to process:", len(run_folders))
    for folder in run_folders:
        logger.info("  • %s", folder.name)

    existing_rows, existing_keys = _load_existing_consolidated()

    new_count = 0
    skipped_duplicate = 0

    for run_folder in run_folders:
        logger.info("Processing: %s", run_folder.name)
        columns = _read_run_columns(run_folder)

        for row in columns:
            key = _dedup_key(row)
            if key in existing_keys:
                skipped_duplicate += 1
                continue

            existing_rows.append(row)
            existing_keys.add(key)
            new_count += 1

    _write_consolidated_csv(existing_rows)
    _write_consolidated_yaml(existing_rows)

    if DREMIO_PUBLISH_AUTO and DREMIO_PUBLISH_HOME_NAME and DREMIO_PUBLISH_DATASET_NAME:
        client = DremioClient()
        published = client.publish_home_csv(
            CONSOLIDATED_CSV,
            DREMIO_PUBLISH_HOME_NAME,
            DREMIO_PUBLISH_DATASET_NAME,
            DREMIO_PUBLISH_FOLDER_PATH,
        )
        if not published:
            logger.warning("Consolidated CSV was written locally, but Dremio publish failed.")
    else:
        logger.info(
            "Dremio publish skipped (set DREMIO_PUBLISH_HOME_NAME and DREMIO_PUBLISH_DATASET_NAME to enable it)."
        )

    logger.info("═" * 60)
    logger.info("Consolidation complete:")
    logger.info("  New rows added:     %d", new_count)
    logger.info("  Duplicates skipped: %d", skipped_duplicate)
    logger.info("  Total consolidated: %d", len(existing_rows))
    logger.info("═" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Consolidate DQ run outputs into a single reporting file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="all_dates",
        help="Process ALL historical runs (not just today).",
    )
    args = parser.parse_args()
    consolidate(all_dates=args.all_dates)


if __name__ == "__main__":
    main()