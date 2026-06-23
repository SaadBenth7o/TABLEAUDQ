"""
run_dq.py
─────────
Entry point for the Dremio DQ POC runner.

Usage
─────
  # 1. First time (or after the Excel file changes): generate the config
  python lib/run_dq.py --refresh-config

  # 2. Every subsequent run: execute checks and write YAML output
  python lib/run_dq.py

  # 3. Both in one shot (refresh config then run)
  python lib/run_dq.py --refresh-config --run

CLI flags
─────────
  --refresh-config   Parse the Excel file and regenerate checks_config.yaml.
                     Without --run the script stops after config generation.
  --run              Execute DQ checks and write YAML output.
                     This is the default when --refresh-config is NOT passed.
  --output-dir DIR   Override the base output directory (default: ./output).
  --log-level LEVEL  Logging verbosity: DEBUG | INFO | WARNING (default: INFO).

Output
──────
  output/
  └── YYYY-MM-DD_HHMMSS/
      ├── run.log
      ├── _all_tables.yaml
      ├── professional_description.yaml
      └── …
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env")


def _configure_logging(level_name: str, log_file: Path | None = None) -> None:
    """
    Set up root logger with a console handler and an optional file handler.

    Args:
        level_name: One of DEBUG, INFO, WARNING, ERROR.
        log_file:   If provided, also write logs to this file path.
    """
    level = getattr(logging, level_name.upper(), logging.INFO)
    fmt = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)


logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dremio Data Quality POC — Completeness Checker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--refresh-config",
        action="store_true",
        help="(Re)generate checks_config.yaml from the Excel source file.",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help=(
            "Execute DQ checks and write YAML output. "
            "Implied when --refresh-config is NOT passed."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT_DIR / "output",
        metavar="DIR",
        help="Base directory for run output folders (default: ./output).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )
    return parser.parse_args()


_FLAG_ICON = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗", "ERROR": "?"}


def _print_summary(run_result, output_dir: Path) -> None:
    """Print a formatted summary table to stdout after the run."""
    print("\n" + "═" * 80)
    print(f"  DQ Run  —  {run_result.run_timestamp}")
    print("═" * 80)
    print(f"  {'Table':<40} {'Score':>7}  {'Flag':<6}  {'Rows':>8}  Checks  Errors")
    print("  " + "─" * 76)

    for tname, tres in run_result.tables.items():
        icon = _FLAG_ICON.get(tres.table_flag, "?")
        score_str = f"{tres.table_score_pct:.1f}%" if tres.table_score_pct is not None else "  N/A"
        rows_str = f"{tres.total_lignes:,}" if tres.total_lignes is not None else "   N/A"
        nb = len(tres.columns)
        nb_errors = tres.error_count
        print(f"  {tname:<40} {score_str:>7}  {icon} {tres.table_flag:<4}  {rows_str:>8}  {nb:>6}  {nb_errors:>6}")

    print("═" * 80)
    print(f"\n  Output → {output_dir.resolve()}\n")


def main() -> None:
    args = _parse_args()

    do_run: bool = args.run or (not args.refresh_config)

    run_ts_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    output_dir = args.output_dir / run_ts_str
    output_dir.mkdir(parents=True, exist_ok=True)

    _configure_logging(args.log_level, log_file=output_dir / "run.log")

    logger.info("═" * 60)
    logger.info("Dremio DQ POC  —  starting  (ts=%s)", run_ts_str)
    logger.info("═" * 60)

    if args.refresh_config:
        from lib.excel_parser import generate_config

        logger.info("Refreshing checks_config.yaml from Excel…")
        config_path = generate_config()
        logger.info("Config generated: %s", config_path)

        if not do_run:
            logger.info("--run not passed → stopping after config generation.")
            print(f"\n✓ checks_config.yaml generated at: {config_path}")
            return

    from lib.excel_parser import load_config

    try:
        config = load_config()
        logger.info(
            "Config loaded: %d tables, %d unique queries",
            len(config.get("tables", {})),
            len(config.get("unique_queries", {})),
        )
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    from lib.dq_runner import run_dq_checks

    logger.info("Starting DQ checks…")
    run_result = run_dq_checks(config)

    from lib.yaml_writer import write_results as write_yaml

    logger.info("Writing YAML output to: %s", output_dir)
    write_yaml(run_result, output_dir)

    from lib.csv_writer import write_results as write_csv

    logger.info("Writing CSV output to: %s", output_dir)
    write_csv(run_result, output_dir)

    _print_summary(run_result, output_dir)
    logger.info("Run complete.")


if __name__ == "__main__":
    main()