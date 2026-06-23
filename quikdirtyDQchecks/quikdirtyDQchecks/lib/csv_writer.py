"""
csv_writer.py
─────────────
Export DQ results to CSV format (per-table + global summary).

Exports:
  - {timestamp}/{table}.csv — one file per table with column details
  - {timestamp}/_all_tables.csv — global summary per table
"""

import csv
import logging
from datetime import datetime
from pathlib import Path

from .dq_runner import RunResult

logger = logging.getLogger(__name__)


def write_results(run_result: RunResult, output_dir: Path) -> None:
    """
    Write RunResult to CSV files (per-table + global summary).

    Creates:
      - output_dir/{timestamp}_csv/<table>.csv per Virtualisation table.
      - output_dir/{timestamp}_csv/_all_tables.csv global summary.

    Args:
        run_result: The aggregated DQ run result with table/column scores.
        output_dir: Directory where subdirectory will be created (must already exist).
    """
    # Create subdirectory for CSV files
    timestamp_name = output_dir.name
    csv_dir = output_dir / f"{timestamp_name}_csv"
    csv_dir.mkdir(parents=True, exist_ok=True)

    # ── Write per-table CSV files ──────────────────────────────────────────────

    for table_name, table_result in run_result.tables.items():
        csv_path = csv_dir / f"{table_name}.csv"

        try:
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=[
                        "dremio_col",
                        "virt_full_path",
                        "dataset",
                        "domain",
                        "rule",
                        "total_lignes",
                        "valides",
                        "score_pct",
                        "flag",
                        "error",
                        "timestamp",
                    ],
                )
                writer.writeheader()

                for col in table_result.columns:
                    writer.writerow(
                        {
                            "dremio_col": col.dremio_col or "",
                            "virt_full_path": col.virt_full_path or "",
                            "dataset": col.dataset or "",
                            "domain": col.domain or "",
                            "rule": col.rule or "",
                            "total_lignes": col.total_lignes or 0,
                            "valides": col.valides or 0,
                            "score_pct": f"{col.score_pct:.2f}" if col.score_pct is not None else "N/A",
                            "flag": col.flag or "ERROR",
                            "error": col.error or "",
                            "timestamp": run_result.run_timestamp,
                        }
                    )

            logger.info("Per-table CSV written: %s", csv_path)

        except Exception as exc:
            logger.error("Failed to write CSV %s: %s", csv_path, exc)

    # ── Write global summary CSV ───────────────────────────────────────────────

    global_csv_path = csv_dir / "_all_tables.csv"

    try:
        with open(global_csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "table",
                    "total_lignes",
                    "score_pct",
                    "flag",
                    "nb_checks",
                    "nb_pass",
                    "nb_warn",
                    "nb_fail",
                    "nb_error",
                    "timestamp",
                ],
            )
            writer.writeheader()

            for table_name, table_result in run_result.tables.items():
                writer.writerow(
                    {
                        "table": table_name,
                        "total_lignes": table_result.total_lignes or 0,
                        "score_pct": f"{table_result.table_score_pct:.2f}" if table_result.table_score_pct is not None else "N/A",
                        "flag": table_result.table_flag or "ERROR",
                        "nb_checks": len(table_result.columns),
                        "nb_pass": sum(1 for c in table_result.columns if c.flag == "PASS"),
                        "nb_warn": sum(1 for c in table_result.columns if c.flag == "WARN"),
                        "nb_fail": sum(1 for c in table_result.columns if c.flag == "FAIL"),
                        "nb_error": sum(1 for c in table_result.columns if c.flag == "ERROR"),
                        "timestamp": run_result.run_timestamp,
                    }
                )

        logger.info("Global summary CSV written: %s", global_csv_path)

    except Exception as exc:
        logger.error("Failed to write global CSV %s: %s", global_csv_path, exc)
