"""
yaml_writer.py
──────────────
Serialises a `RunResult` (produced by dq_runner.py) to YAML files.

Output structure for each run (inside a timestamped folder):
  output/
  └── 2026-06-03_120000/
      ├── _all_tables.yaml               ← global summary across all tables
      ├── professional_description.yaml  ← one file per Virtualisation table
      ├── natural_person_description.yaml
      ├── legal_entity_description.yaml
      ├── legal_entity_financial_data.yaml
      ├── professional_activity.yaml
      └── customers.yaml

Per-table YAML structure:
  table:             <table_name>
  run_timestamp:     "2026-06-03T12:00:00"
  total_lignes:      12345
  table_score_pct:   87.4
  table_flag:        WARN
  columns:
    - dremio_col:    chiffre_affaire
      virt_col:      turnover
      dataset:       CIHOne.CLIENTS.Professionnels.clients
      domain:        PTP-TIE-POR
      rule:          Completude
      total_lignes:  12345
      valides:       10789
      score_pct:     87.4
      flag:          WARN
      error:         null

Global YAML (_all_tables.yaml) structure:
  run_timestamp: "2026-06-03T12:00:00"
  global_score_pct: 88.1
  global_flag:      WARN
  tables:
    - table:            professional_description
      total_lignes:     12345
      table_score_pct:  87.4
      table_flag:       WARN
      nb_checks:        8
      nb_pass:          5
      nb_warn:          2
      nb_fail:          1
      nb_error:         0
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict

import yaml

from .dq_runner import ColumnResult, RunResult, TableResult, _compute_flag

logger = logging.getLogger(__name__)


# ── Serialisation helpers ─────────────────────────────────────────────────────

def _column_to_dict(col: ColumnResult, timestamp: str) -> dict:
    """Serialise a ColumnResult to a plain dict suitable for YAML output."""
    return {
        "dremio_col": col.dremio_col,
        "virt_col": col.virt_col,
        "virt_full_path": col.virt_full_path,
        "dataset": col.dataset,
        "domain": col.domain,
        "rule": col.rule,
        "total_lignes": col.total_lignes,
        "valides": col.valides,
        "score_pct": col.score_pct,
        "flag": col.flag,
        "error": col.error,
        "timestamp": timestamp,
    }


def _table_to_dict(table: TableResult, run_ts: str, timestamp: str) -> dict:
    """Serialise a TableResult (with all its columns) to a plain dict."""
    return {
        "table": table.table_name,
        "run_timestamp": run_ts,
        "total_lignes": table.total_lignes,
        "table_score_pct": table.table_score_pct,
        "table_flag": table.table_flag,
        "columns": [_column_to_dict(c, timestamp) for c in table.columns],
    }


def _table_summary(table: TableResult, timestamp: str) -> dict:
    """Build a compact summary row for the global _all_tables.yaml."""
    flag_counts: Dict[str, int] = {"PASS": 0, "WARN": 0, "FAIL": 0, "ERROR": 0}
    for col in table.columns:
        flag_counts[col.flag] = flag_counts.get(col.flag, 0) + 1

    return {
        "table": table.table_name,
        "total_lignes": table.total_lignes,
        "table_score_pct": table.table_score_pct,
        "table_flag": table.table_flag,
        "nb_checks": len(table.columns),
        "nb_pass": flag_counts.get("PASS", 0),
        "nb_warn": flag_counts.get("WARN", 0),
        "nb_fail": flag_counts.get("FAIL", 0),
        "nb_error": flag_counts.get("ERROR", 0),
        "timestamp": timestamp,
    }


def _global_score(run: RunResult) -> tuple[float | None, str]:
    """
    Compute an overall score as the average of all table scores.

    Returns:
        (global_score_pct, global_flag) — score is None if no table has data.
    """
    valid_scores = [
        t.table_score_pct
        for t in run.tables.values()
        if t.table_score_pct is not None
    ]
    if not valid_scores:
        return None, "ERROR"
    score = round(sum(valid_scores) / len(valid_scores), 2)
    return score, _compute_flag(score)


# ── YAML dump helper ──────────────────────────────────────────────────────────

def _write_yaml(data: dict, path: Path) -> None:
    """Write *data* as a YAML file at *path* using UTF-8 encoding."""
    with open(path, "w", encoding="utf-8") as fh:
        yaml.dump(data, fh, allow_unicode=True, sort_keys=False, default_flow_style=False)
    logger.debug("Written: %s", path)


# ── Public interface ──────────────────────────────────────────────────────────

def write_results(run: RunResult, output_dir: Path) -> None:
    """
    Write all YAML output files for a completed DQ run.

    Creates:
      - output_dir/{timestamp}_yaml/<table_name>.yaml per Virtualisation table.
      - output_dir/{timestamp}_yaml/_all_tables.yaml global summary.

    Args:
        run:        RunResult object from dq_runner.run_dq_checks().
        output_dir: Directory where subdirectory will be created (must already exist).
    """
    # Create subdirectory for YAML files
    timestamp_name = output_dir.name
    yaml_dir = output_dir / f"{timestamp_name}_yaml"
    yaml_dir.mkdir(parents=True, exist_ok=True)
    
    # ── Per-table files ───────────────────────────────────────────────────────
    for table_name, table_res in run.tables.items():
        data = _table_to_dict(table_res, run.run_timestamp, run.run_timestamp)
        file_path = yaml_dir / f"{table_name}.yaml"
        _write_yaml(data, file_path)
        logger.info("  [table] %s → %s", table_name, file_path.name)

    # ── Global summary ────────────────────────────────────────────────────────
    global_score, global_flag = _global_score(run)
    global_data = {
        "run_timestamp": run.run_timestamp,
        "global_score_pct": global_score,
        "global_flag": global_flag,
        "nb_tables": len(run.tables),
        "tables": [_table_summary(t, run.run_timestamp) for t in run.tables.values()],
    }
    global_path = yaml_dir / "_all_tables.yaml"
    _write_yaml(global_data, global_path)
    logger.info("  [global] → %s", global_path.name)
    logger.info(
        "All YAML files written to: %s  (global_score=%.1f%%  flag=%s)",
        yaml_dir,
        global_score or 0,
        global_flag,
    )
