"""
dq_runner.py
────────────
Orchestrates the execution of DQ checks against Dremio and computes scores.

Responsibilities:
  1. Load the `checks_config.yaml` produced by `excel_parser.py`.
  2. Execute each *unique* SQL query exactly once via `DremioClient`.
  3. Map every result back to all checks that share the same SQL (query_id).
  4. Compute per-column flag  : PASS / WARN / FAIL based on score thresholds.
  5. Compute per-table score  : average of all column scores for that table.
  6. Compute per-table flag   : same threshold logic applied to the table score.
  7. Return a structured `RunResult` ready to be serialised by `yaml_writer.py`.

Thresholds (configurable via .env):
  SCORE_PASS_THRESHOLD  (default 90)  → flag = PASS
  SCORE_WARN_THRESHOLD  (default 70)  → flag = WARN  (below PASS)
                                         flag = FAIL  (below WARN)
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

try:
    from .dremio_client import DremioClient
    from .excel_parser import load_config
except ImportError:
    # Allow direct execution: python lib/dq_runner.py
    ROOT_DIR = Path(__file__).resolve().parent.parent
    if str(ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(ROOT_DIR))
    from lib.dremio_client import DremioClient
    from lib.excel_parser import load_config

load_dotenv()

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────

SCORE_PASS_THRESHOLD: float = float(os.getenv("SCORE_PASS_THRESHOLD", 90))
SCORE_WARN_THRESHOLD: float = float(os.getenv("SCORE_WARN_THRESHOLD", 70))


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class ColumnResult:
    """DQ result for a single Excel check row (one column in one dataset)."""

    domain: str
    dataset: str
    dremio_col: str
    virt_col: str
    virt_full_path: str
    raw_col: str
    rule: str
    query_id: str

    # Populated after query execution
    total_lignes: Optional[int] = None
    valides: Optional[int] = None
    score_pct: Optional[float] = None
    flag: str = "ERROR"          # PASS | WARN | FAIL | ERROR (query failed)
    error: Optional[str] = None  # error message if query failed


@dataclass
class TableResult:
    """Aggregated DQ result for one Virtualisation table."""

    table_name: str
    columns: List[ColumnResult] = field(default_factory=list)

    # Populated after all columns are computed
    total_lignes: Optional[int] = None   # max total_lignes across columns (table row count)
    table_score_pct: Optional[float] = None
    table_flag: str = "ERROR"
    error_count: int = 0  # number of checks that failed with ERROR flag


@dataclass
class RunResult:
    """Full result of one DQ run across all tables."""

    run_timestamp: str                          # ISO-8601 string
    tables: Dict[str, TableResult] = field(default_factory=dict)


# ── Flag computation ──────────────────────────────────────────────────────────

def _compute_flag(score: Optional[float]) -> str:
    """
    Convert a percentage score to a flag string.

    Args:
        score: Completeness score in [0, 100], or None if query failed.

    Returns:
        "PASS", "WARN", "FAIL", or "ERROR" if score is None.
    """
    if score is None:
        return "ERROR"
    if score >= SCORE_PASS_THRESHOLD:
        return "PASS"
    if score >= SCORE_WARN_THRESHOLD:
        return "WARN"
    return "FAIL"


# ── Result extraction from Dremio row ─────────────────────────────────────────

def _extract_metrics(row: Optional[Dict[str, Any]]) -> tuple[Optional[int], Optional[int], Optional[float]]:
    """
    Extract (total_lignes, valides, score_completude_pct) from a Dremio result row.

    The DQ SQL queries in the config always return these three columns.
    Column names are matched case-insensitively.

    Args:
        row: Dict returned by DremioClient.run_query(), or None on failure.

    Returns:
        Tuple of (total_lignes, valides, score_pct), each may be None on failure.
    """
    if not row:
        return None, None, None

    # Normalise keys to lowercase for safe lookup
    normalised = {k.lower(): v for k, v in row.items()}

    total = normalised.get("total_lignes")
    valides = normalised.get("valides")
    score = normalised.get("score_completude_pct")

    return (
        int(total) if total is not None else None,
        int(valides) if valides is not None else None,
        round(float(score), 2) if score is not None else None,
    )


# ── Main runner ───────────────────────────────────────────────────────────────

def run_dq_checks(config: Optional[dict] = None) -> RunResult:
    """
    Execute all DQ checks defined in `checks_config.yaml` and return a `RunResult`.

    Each unique SQL query is executed exactly once via the DremioClient.
    Results are then mapped back to every check that shares the same query_id.

    Args:
        config: Pre-loaded config dict.  If None, `load_config()` is called.

    Returns:
        RunResult populated with per-column and per-table scores and flags.
    """
    if config is None:
        config = load_config()

    run_ts = datetime.now().isoformat(timespec="seconds")
    result = RunResult(run_timestamp=run_ts)

    client = DremioClient()

    # ── Step 1: execute unique queries ────────────────────────────────────────
    unique_queries: Dict[str, Dict] = config.get("unique_queries", {})
    query_results: Dict[str, Optional[Dict[str, Any]]] = {}

    logger.info("Executing %d unique SQL queries…", len(unique_queries))
    for query_id, qdata in unique_queries.items():
        sql: str = qdata["sql"]
        logger.info("  Running query_id=%s", query_id)
        query_results[query_id] = client.run_query(sql)

    # ── Step 2: map results to checks and build table results ─────────────────
    tables_config: Dict[str, Dict] = config.get("tables", {})

    for table_name, tdata in tables_config.items():
        table_res = TableResult(table_name=table_name)
        checks = tdata.get("checks", [])

        for check in checks:
            query_id: str = check["query_id"]
            raw_row = query_results.get(query_id)
            total, valides, score = _extract_metrics(raw_row)

            col_res = ColumnResult(
                domain=check.get("domain", ""),
                dataset=check.get("dataset", ""),
                dremio_col=check.get("dremio_col", ""),
                virt_col=check.get("virt_col", ""),
                virt_full_path=check.get("virt_full_path", ""),
                raw_col=check.get("raw_col", ""),
                rule=check.get("rule", ""),
                query_id=query_id,
                total_lignes=total,
                valides=valides,
                score_pct=score,
                flag=_compute_flag(score),
                error="Query returned no data" if raw_row is None else None,
            )
            table_res.columns.append(col_res)

        # ── Step 3: compute table-level aggregates ────────────────────────────
        # Include scores from all checks: ERROR checks (failed queries) count as 0%
        all_scores = []
        error_count = 0
        for c in table_res.columns:
            if c.score_pct is not None:
                all_scores.append(c.score_pct)
            elif c.flag == "ERROR":
                all_scores.append(0)  # Treat query failures as 0% completeness
                error_count += 1

        # Store error count in table result
        table_res.error_count = error_count

        if error_count > 0:
            logger.warning(
                "  Table %s: %d/%d checks failed (counted as 0%%)",
                table_name,
                error_count,
                len(table_res.columns),
            )

        valid_totals = [c.total_lignes for c in table_res.columns if c.total_lignes is not None]

        if all_scores:
            table_res.table_score_pct = round(sum(all_scores) / len(all_scores), 2)
            table_res.table_flag = _compute_flag(table_res.table_score_pct)
        else:
            table_res.table_flag = "ERROR"

        # Use the max total_lignes across columns as the "table row count"
        # (all columns in the same Virtualisation table share the same base table)
        table_res.total_lignes = max(valid_totals) if valid_totals else None

        result.tables[table_name] = table_res
        logger.info(
            "  Table %-40s  score=%s%%  flag=%s  rows=%s",
            table_name,
            table_res.table_score_pct,
            table_res.table_flag,
            table_res.total_lignes,
        )

    # ── Summary: count total errors across all tables ────────────────────────
    global_error_count = sum(
        sum(1 for c in tres.columns if c.flag == "ERROR")
        for tres in result.tables.values()
    )
    if global_error_count > 0:
        logger.warning(
            "⚠  %d check(s) failed globally. Scores reflect failures as 0%%.",
            global_error_count,
        )

    logger.info("DQ run complete.  Timestamp: %s", run_ts)
    return result


if __name__ == "__main__":
    import logging.config
    from datetime import datetime
    
    try:
        from .yaml_writer import write_results
    except ImportError:
        from lib.yaml_writer import write_results
    
    # ── Configure logging ─────────────────────────────────────────────────────
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    
    try:
        result = run_dq_checks()
        
        # Create timestamped output directory
        ts_name = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        output_base = Path(__file__).resolve().parent.parent / "output" / ts_name
        output_base.mkdir(parents=True, exist_ok=True)
        
        write_results(result, output_base)
        logger.info("Run results serialised.")
    except Exception as exc:
        logger.exception("Fatal error during DQ run: %s", exc)
        exit(1)
