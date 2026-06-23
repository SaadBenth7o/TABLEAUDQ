"""
dremio_client.py
────────────────
Handles all communication with the Dremio REST API.

Responsibilities:
  1. Authentication:
       - "bearer" mode  → sends `Authorization: Bearer <DREMIO_API_KEY>` header.
       - "legacy" mode  → authenticates via POST /apiv2/login (username + password)
                          and uses the returned `_dremio<token>` format.
  2. SQL submission → POST /api/v3/sql
  3. Job polling    → GET  /api/v3/job/{jobId}  (until COMPLETED or FAILED)
  4. Result fetch   → GET  /api/v3/job/{jobId}/results
  5. Returns a plain dict with keys matching the SQL column aliases
     (total_lignes, valides, score_completude_pct).

Environment variables consumed (loaded from .env):
  DREMIO_HOST          Full base URL, e.g. http://dlakegtwprd:9047
  DREMIO_AUTH_TYPE     "bearer" (default) or "legacy"
  DREMIO_API_KEY       PAT token — used when DREMIO_AUTH_TYPE=bearer
  DREMIO_USERNAME      Used when DREMIO_AUTH_TYPE=legacy
  DREMIO_PASSWORD      Used when DREMIO_AUTH_TYPE=legacy
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

DREMIO_HOST: str = os.getenv("DREMIO_HOST", "http://dlakegtwprd:9047").rstrip("/")
DREMIO_AUTH_TYPE: str = os.getenv("DREMIO_AUTH_TYPE", "bearer").lower()
DREMIO_API_KEY: str = os.getenv("DREMIO_API_KEY", "")
DREMIO_USERNAME: str = os.getenv("DREMIO_USERNAME", "")
DREMIO_PASSWORD: str = os.getenv("DREMIO_PASSWORD", "")

# Polling behaviour
POLL_INTERVAL_SEC: float = 2.0      # seconds between job status checks
JOB_TIMEOUT_SEC: float = 300.0     # max seconds to wait for a single query

# Terminal job states
_TERMINAL_STATES = {"COMPLETED", "CANCELED", "FAILED"}


# ── Authentication ────────────────────────────────────────────────────────────

class DremioAuthError(Exception):
    """Raised when authentication with Dremio fails."""


def _get_auth_header() -> Dict[str, str]:
    """
    Build and return the `Authorization` header dict for the configured auth mode.

    For "bearer" mode the PAT is used directly.
    For "legacy" mode a login call is made and the returned token is used.

    Returns:
        Dict with a single "Authorization" key.

    Raises:
        DremioAuthError: If credentials are missing or the login call fails.
    """
    if DREMIO_AUTH_TYPE == "bearer":
        if not DREMIO_API_KEY:
            raise DremioAuthError("DREMIO_API_KEY is not set in .env")
        return {"Authorization": f"Bearer {DREMIO_API_KEY}"}

    if DREMIO_AUTH_TYPE == "legacy":
        if not DREMIO_USERNAME or not DREMIO_PASSWORD:
            raise DremioAuthError(
                "DREMIO_USERNAME and DREMIO_PASSWORD must be set for DREMIO_AUTH_TYPE=legacy"
            )
        token = _legacy_login(DREMIO_USERNAME, DREMIO_PASSWORD)
        return {"Authorization": f"_dremio{token}"}

    raise DremioAuthError(
        f"Unknown DREMIO_AUTH_TYPE='{DREMIO_AUTH_TYPE}'. Use 'bearer' or 'legacy'."
    )


def _legacy_login(username: str, password: str) -> str:
    """
    Authenticate against Dremio's /apiv2/login endpoint.

    Args:
        username: Dremio username.
        password: Dremio password.

    Returns:
        Token string to be prefixed with `_dremio` in the Authorization header.

    Raises:
        DremioAuthError: If the login request fails.
    """
    url = f"{DREMIO_HOST}/apiv2/login"
    payload = {"userName": username, "password": password}

    try:
        resp = requests.post(url, json=payload, timeout=15)
    except requests.RequestException as exc:
        raise DremioAuthError(f"Login request failed: {exc}") from exc

    if resp.status_code != 200:
        raise DremioAuthError(
            f"Login failed (HTTP {resp.status_code}): {resp.text[:300]}"
        )

    token = resp.json().get("token")
    if not token:
        raise DremioAuthError("Login response did not contain a token.")

    logger.debug("Legacy login successful for user '%s'", username)
    return token


# ── Dremio REST helpers ───────────────────────────────────────────────────────

class DremioQueryError(Exception):
    """Raised when a Dremio SQL job fails or times out."""


def _submit_sql(sql: str, headers: Dict[str, str]) -> str:
    """
    Submit a SQL query to Dremio and return the job ID.

    Args:
        sql:     SQL string to execute.
        headers: HTTP headers including Authorization.

    Returns:
        Dremio job ID string.

    Raises:
        DremioQueryError: If the submission fails.
    """
    url = f"{DREMIO_HOST}/api/v3/sql"
    payload = {"sql": sql}

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
    except requests.RequestException as exc:
        raise DremioQueryError(f"SQL submission request failed: {exc}") from exc

    if resp.status_code not in (200, 201):
        raise DremioQueryError(
            f"SQL submission failed (HTTP {resp.status_code}): {resp.text[:300]}"
        )

    job_id = resp.json().get("id")
    if not job_id:
        raise DremioQueryError("Dremio response did not include a job ID.")

    logger.debug("Submitted SQL → jobId: %s", job_id)
    return job_id


def _poll_job(job_id: str, headers: Dict[str, str]) -> str:
    """
    Poll a Dremio job until it reaches a terminal state.

    Args:
        job_id:  Dremio job identifier.
        headers: HTTP headers including Authorization.

    Returns:
        Final job state string (e.g. "COMPLETED").

    Raises:
        DremioQueryError: If the job fails, is cancelled, or times out.
    """
    url = f"{DREMIO_HOST}/api/v3/job/{job_id}"
    deadline = time.monotonic() + JOB_TIMEOUT_SEC

    while True:
        if time.monotonic() > deadline:
            raise DremioQueryError(
                f"Job {job_id} timed out after {JOB_TIMEOUT_SEC}s"
            )

        try:
            resp = requests.get(url, headers=headers, timeout=15)
        except requests.RequestException as exc:
            raise DremioQueryError(f"Job poll request failed: {exc}") from exc

        if resp.status_code != 200:
            raise DremioQueryError(
                f"Job poll failed (HTTP {resp.status_code}): {resp.text[:300]}"
            )

        state: str = resp.json().get("jobState", "")
        logger.debug("Job %s state: %s", job_id, state)

        if state in _TERMINAL_STATES:
            if state != "COMPLETED":
                error_msg = resp.json().get("errorMessage", "no detail")
                raise DremioQueryError(
                    f"Job {job_id} ended with state '{state}': {error_msg}"
                )
            return state

        time.sleep(POLL_INTERVAL_SEC)


def _fetch_results(job_id: str, headers: Dict[str, str]) -> List[Dict[str, Any]]:
    """
    Retrieve the result rows of a completed Dremio job.

    Args:
        job_id:  Dremio job identifier.
        headers: HTTP headers including Authorization.

    Returns:
        List of row dicts, e.g. [{"total_lignes": 5000, "valides": 4800, ...}].

    Raises:
        DremioQueryError: If the results request fails.
    """
    url = f"{DREMIO_HOST}/api/v3/job/{job_id}/results"

    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException as exc:
        raise DremioQueryError(f"Results fetch request failed: {exc}") from exc

    if resp.status_code != 200:
        raise DremioQueryError(
            f"Results fetch failed (HTTP {resp.status_code}): {resp.text[:300]}"
        )

    data = resp.json()
    rows = data.get("rows", [])
    logger.debug("Job %s returned %d row(s).", job_id, len(rows))
    return rows


# ── Public interface ──────────────────────────────────────────────────────────

class DremioClient:
    """
    Stateless Dremio REST client.

    Usage:
        client = DremioClient()
        result = client.run_query("SELECT COUNT(*) ...")
        # result → {"total_lignes": 5000, "valides": 4800, "score_completude_pct": 96.0}
    """

    def __init__(self) -> None:
        # Resolve auth header once at construction time (one login call for legacy mode)
        self._headers: Dict[str, str] = {
            "Content-Type": "application/json",
            **_get_auth_header(),
        }
        logger.info(
            "DremioClient initialised  host=%s  auth_type=%s",
            DREMIO_HOST,
            DREMIO_AUTH_TYPE,
        )

    def run_query(self, sql: str) -> Optional[Dict[str, Any]]:
        """
        Execute a SQL query on Dremio and return the first result row as a dict.

        The DQ queries in checks_config.yaml each return exactly one row with
        columns: total_lignes, valides, score_completude_pct.

        Args:
            sql: SQL query string.

        Returns:
            Dict of column → value for the first result row, or None on error.
        """
        try:
            job_id = _submit_sql(sql, self._headers)
            _poll_job(job_id, self._headers)
            rows = _fetch_results(job_id, self._headers)
            if not rows:
                logger.warning("Query returned no rows. jobId=%s", job_id)
                return None
            return rows[0]  # DQ queries return a single aggregate row
        except DremioQueryError as exc:
            logger.error("Query execution failed: %s", exc)
            return None

    def execute_sql(self, sql: str) -> bool:
        """
        Execute a SQL statement on Dremio and only wait for completion.

        This is useful for DDL statements (CREATE VIEW, DROP VIEW, etc.) where
        no row payload is required.
        """
        try:
            job_id = _submit_sql(sql, self._headers)
            _poll_job(job_id, self._headers)
            return True
        except DremioQueryError as exc:
            logger.error("SQL execution failed: %s", exc)
            return False

    def publish_home_csv(
        self,
        csv_path: Path,
        home_name: str,
        dataset_name: str,
        folder_path: str = "",
    ) -> bool:
        """
        Upload a CSV file into a Dremio home space and finalize it as a dataset.

        The method follows the same `upload_start` / `upload_finish` flow used by the Dremio UI.

        Args:
            csv_path: Local CSV file to upload.
            home_name: Dremio home name, for example "@S141".
            dataset_name: Target dataset name inside the home space.
            folder_path: Optional folder path inside the home space, for example
                         "TiersDataQualityReport".

        Returns:
            True if the upload and finalization succeeded, False otherwise.
        """
        if not csv_path.exists():
            raise DremioQueryError(f"CSV file not found: {csv_path}")

        # Keep the business dataset name as a typed view and store raw CSV in a
        # technical dataset so the public path remains stable for BI consumers.
        raw_dataset_name = f"{dataset_name}__raw"

        encoded_home = quote(home_name, safe="")
        normalized_folder = self._normalize_folder_path(folder_path)
        encoded_dataset_path = quote(self._join_home_path(normalized_folder, raw_dataset_name), safe="/_-.")
        encoded_business_dataset_path = quote(
            self._join_home_path(normalized_folder, dataset_name), safe="/_-."
        )
        encoded_upload_path = quote(normalized_folder, safe="/_-.")
        start_url = f"{DREMIO_HOST}/apiv2/home/{encoded_home}/upload_start/{encoded_upload_path}"
        finish_url = f"{DREMIO_HOST}/apiv2/home/{encoded_home}/upload_finish/{encoded_dataset_path}"
        file_url = f"{DREMIO_HOST}/apiv2/home/{encoded_home}/file/{encoded_dataset_path}"
        business_file_url = f"{DREMIO_HOST}/apiv2/home/{encoded_home}/file/{encoded_business_dataset_path}"

        multipart_headers = {
            key: value
            for key, value in self._headers.items()
            if key.lower() != "content-type"
        }
        multipart_headers["Accept"] = "application/json"

        json_headers = {
            **self._headers,
            "Accept": "application/json",
        }

        logger.info(
            "Uploading consolidated CSV to Dremio home '%s' folder '%s' as '%s'",
            home_name,
            normalized_folder or "/",
            raw_dataset_name,
        )

        try:
            # Dremio upload_finish does not overwrite an existing file automatically.
            # To ensure idempotent runs, delete the current home file first when present.
            self._delete_home_file_if_exists(file_url, json_headers)
            # If a previous physical file exists with the business name, remove it so
            # the typed view can own that exact dataset path.
            try:
                self._delete_home_file_if_exists(business_file_url, json_headers)
            except DremioQueryError as exc:
                # Some Dremio deployments return HTTP 500 when probing /file on a
                # non-file entity (for example an existing VDS). This must not block
                # publishing the raw CSV and recreating the typed business view.
                logger.warning(
                    "Skipping business-path file cleanup (non-blocking): %s",
                    exc,
                )

            with open(csv_path, "rb") as fh:
                files = {
                    "file": (f"{raw_dataset_name}.csv", fh, "text/csv"),
                }
                data = {"fileName": raw_dataset_name}
                params = {"extension": "csv"}

                resp = requests.post(
                    start_url,
                    headers=multipart_headers,
                    params=params,
                    data=data,
                    files=files,
                    timeout=30,
                )

            if resp.status_code not in (200, 201):
                raise DremioQueryError(
                    f"Upload start failed (HTTP {resp.status_code}): {resp.text[:300]}"
                )

            stage_payload = resp.json()
            file_format = self._extract_file_format(stage_payload)
            if file_format is None:
                raise DremioQueryError(
                    "Upload start response did not contain a usable file format payload."
                )

            # Use the first CSV row as column headers so the dataset lands in Dremio with
            # business-friendly names instead of generated columns.
            file_format["extractHeader"] = True
            file_format["skipFirstLine"] = False
            file_format["autoGenerateColumnNames"] = False
            file_format["trimHeader"] = True

            finish_resp = requests.post(
                finish_url,
                headers=json_headers,
                json=file_format,
                timeout=30,
            )

            if finish_resp.status_code not in (200, 201):
                raise DremioQueryError(
                    f"Upload finish failed (HTTP {finish_resp.status_code}): {finish_resp.text[:300]}"
                )

            logger.info(
                "Dremio raw CSV publish completed: home=%s folder=%s dataset=%s",
                home_name,
                normalized_folder or "/",
                raw_dataset_name,
            )

            typed_ok = self._create_or_replace_typed_view(
                home_name=home_name,
                folder_path=normalized_folder,
                source_dataset_name=raw_dataset_name,
                target_dataset_name=dataset_name,
            )
            if not typed_ok:
                logger.warning(
                    "CSV publish succeeded, but typed view creation failed for dataset '%s'.",
                    dataset_name,
                )
            return True

        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            logger.error("Dremio publish failed: %s", exc)
            return False
        except DremioQueryError as exc:
            logger.error("Dremio publish failed: %s", exc)
            return False

    def _delete_home_file_if_exists(self, file_url: str, headers: Dict[str, str]) -> None:
        """
        Delete an existing home file before publishing, if it already exists.

        Dremio requires a version query parameter to delete files, so we first GET metadata,
        then DELETE with `version=<...>`.
        """
        get_resp = requests.get(file_url, headers=headers, timeout=30)
        if get_resp.status_code == 404:
            logger.info("No existing Dremio home file found at publish target; creating a new one.")
            return
        if get_resp.status_code != 200:
            raise DremioQueryError(
                f"Cannot inspect existing home file (HTTP {get_resp.status_code}): {get_resp.text[:300]}"
            )

        metadata = get_resp.json()
        version = (
            metadata.get("fileFormat", {})
            .get("fileFormat", {})
            .get("version")
        )
        if not version:
            raise DremioQueryError("Existing home file has no version; cannot delete for overwrite.")

        logger.info("Existing Dremio home file found; deleting it before overwrite publish.")

        del_resp = requests.delete(
            file_url,
            headers=headers,
            params={"version": version},
            timeout=30,
        )
        if del_resp.status_code not in (200, 202, 204, 404):
            raise DremioQueryError(
                f"Failed to delete existing home file (HTTP {del_resp.status_code}): {del_resp.text[:300]}"
            )

        logger.info("Existing Dremio home file deleted successfully.")

    @staticmethod
    def _normalize_folder_path(folder_path: str) -> str:
        """Return a clean home-folder path without leading or trailing slashes."""
        return "/".join(part for part in folder_path.strip("/").split("/") if part)

    @staticmethod
    def _join_home_path(folder_path: str, dataset_name: str) -> str:
        """Join optional folder path and dataset name into a single Dremio home-relative path."""
        if not folder_path:
            return dataset_name
        return f"{folder_path}/{dataset_name}"

    @staticmethod
    def _extract_file_format(payload: Dict[str, Any]) -> Dict[str, Any] | None:
        """
        Extract the nested file format object returned by the upload_start endpoint.

        The UI passes the inner file format object to upload_finish; the exact response shape can
        differ a bit between versions, so we try a few sensible patterns.
        """
        if not isinstance(payload, dict):
            return None

        file_format = payload.get("fileFormat")
        if isinstance(file_format, dict):
            nested = file_format.get("fileFormat")
            if isinstance(nested, dict):
                return nested
            return file_format

        nested_file = payload.get("file")
        if isinstance(nested_file, dict):
            nested = nested_file.get("fileFormat")
            if isinstance(nested, dict):
                return nested

        return None

    def _create_or_replace_typed_view(
        self,
        home_name: str,
        folder_path: str,
        source_dataset_name: str,
        target_dataset_name: str,
    ) -> bool:
        """
        Create/update a typed VDS from the uploaded CSV dataset.

        The raw uploaded file remains untouched; the view applies explicit CASTs
        so BI tools read proper numeric/timestamp types instead of VARCHAR.
        """
        source_ref = self._sql_ref(home_name, folder_path, source_dataset_name)
        typed_ref = self._sql_ref(home_name, folder_path, target_dataset_name)

        sql = f"""
CREATE OR REPLACE VIEW {typed_ref} AS
SELECT
  \"dremio_col\",
  \"virt_full_path\",
  \"dataset\",
  \"domain\",
  \"rule\",
  CAST(NULLIF(\"total_lignes\", '') AS BIGINT) AS \"total_lignes\",
  CAST(NULLIF(\"valides\", '') AS BIGINT) AS \"valides\",
  CAST(NULLIF(\"score_pct\", '') AS DOUBLE) AS \"score_pct\",
  \"flag\",
    TO_TIMESTAMP(
        NULLIF(REPLACE(\"timestamp\", 'T', ' '), ''),
        'YYYY-MM-DD HH24:MI:SS'
    ) AS \"timestamp\"
FROM {source_ref}
""".strip()

        ok = self.execute_sql(sql)
        if ok:
            logger.info(
                "Typed view ready: %s (source=%s)",
                typed_ref,
                source_ref,
            )
        return ok

    @staticmethod
    def _sql_quote(identifier: str) -> str:
        escaped = identifier.replace('"', '""')
        return f'"{escaped}"'

    def _sql_ref(self, home_name: str, folder_path: str, dataset_name: str) -> str:
        parts = [home_name]
        if folder_path:
            parts.extend(part for part in folder_path.split("/") if part)
        parts.append(dataset_name)
        return ".".join(self._sql_quote(part) for part in parts)


# ── CLI entry point (for standalone connectivity test) ────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    client = DremioClient()
    test_sql = "SELECT 1 AS total_lignes, 1 AS valides, 100.0 AS score_completude_pct"
    result = client.run_query(test_sql)
    print("\nTest query result:", result)
