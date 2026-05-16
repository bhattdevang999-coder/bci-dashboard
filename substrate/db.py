"""Atlas substrate \u2014 Postgres connection management.

Lazy-initialised connection pool. The substrate stays usable in two modes:

  Postgres mode:  ATLAS_DATABASE_URL or DATABASE_URL env var is set. Logger
                  writes through the pool. This is production on Render.

  JSONL fallback: env vars unset OR pool init fails. Logger keeps writing
                  to local .jsonl files. This is the test environment and
                  also a graceful degradation if the database is unavailable
                  at app start.

Why a fallback at all? Two reasons:
  1. The 23 existing substrate tests run against tmp dirs. Forcing them to
     stand up a Postgres instance on every test run is hostile.
  2. If the Render Postgres goes down briefly, NIS generation must not
     crash. Substrate writes are best-effort by design \u2014 falling back to
     local JSONL preserves that contract.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

logger = logging.getLogger("atlas.substrate.db")

_POOL = None
_POOL_LOCK = threading.Lock()
_INIT_FAILED = False  # If first init fails we don't retry on every call.


def _database_url() -> Optional[str]:
    """Return the connection string, or None if unset.

    Honours ATLAS_DATABASE_URL first (Atlas-specific) then DATABASE_URL
    (Render's default). Empty strings are treated as unset.
    """
    for key in ("ATLAS_DATABASE_URL", "DATABASE_URL"):
        v = os.environ.get(key)
        if v and v.strip():
            return v.strip()
    return None


def get_pool():
    """Return a connection pool, lazily creating it on first call.

    Returns None if no DATABASE_URL is set, OR if pool creation fails.
    Callers must handle None and fall back to JSONL writes.
    """
    global _POOL, _INIT_FAILED

    if _INIT_FAILED:
        return None
    if _POOL is not None:
        return _POOL

    url = _database_url()
    if not url:
        return None

    with _POOL_LOCK:
        if _POOL is not None:
            return _POOL
        if _INIT_FAILED:
            return None
        try:
            # Import inside the function so test environments without
            # psycopg installed don't crash at module load.
            from psycopg_pool import ConnectionPool  # type: ignore
        except ImportError:
            try:
                # psycopg 3.3+ moved ConnectionPool into a separate package.
                from psycopg.pool import ConnectionPool  # type: ignore
            except ImportError:
                logger.warning("psycopg pool not importable; falling back to JSONL")
                _INIT_FAILED = True
                return None

        try:
            # Pool tuning. Render's Postgres closes idle connections after
            # ~5 minutes; without `check=` the pool will hand out stale
            # connections that time out at the application layer, which is
            # what produced the 'couldn't get a connection after 10s' error
            # on the wizard.
            from psycopg_pool import ConnectionPool as _CP  # type: ignore
            _POOL = ConnectionPool(
                conninfo=url,
                min_size=1,
                max_size=10,
                timeout=10.0,
                max_lifetime=10 * 60,           # rotate connections every 10 min
                max_idle=5 * 60,                # close idle conns after 5 min
                check=_CP.check_connection,     # health-check before handing out
                kwargs={"connect_timeout": 5},
            )
            # Force the pool to validate the connection synchronously \u2014
            # if Postgres is unreachable we want to know now, not on the
            # first write under load.
            with _POOL.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            logger.info("Atlas substrate Postgres pool ready")
        except Exception as exc:
            logger.warning(
                "Atlas substrate Postgres pool init failed: %s. Falling back to JSONL.",
                exc,
            )
            _INIT_FAILED = True
            _POOL = None
            return None
    return _POOL


def is_postgres_active() -> bool:
    """Quick check used by callers to pick a backend without forcing init.

    Returns True only if the pool already exists and is healthy.
    """
    return _POOL is not None and not _INIT_FAILED


def reset_pool_for_tests() -> None:
    """Test helper. Tear down and re-init the pool. Never call from app code."""
    global _POOL, _INIT_FAILED
    with _POOL_LOCK:
        if _POOL is not None:
            try:
                _POOL.close()
            except Exception:
                pass
        _POOL = None
        _INIT_FAILED = False


def wipe_substrate_for_tests() -> None:
    """Test helper. TRUNCATE every substrate table when Postgres is active.

    No-op if no pool. Used by the per-test isolation decorator so each test
    starts clean against either backend without changing the test code.
    """
    pool = get_pool()
    if pool is None:
        return
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                TRUNCATE
                    substrate_events,
                    substrate_sessions,
                    operators,
                    image_library,
                    image_asin_links,
                    outcome_events,
                    rule_library,
                    brand_profile,
                    ingestion_records,
                    keyword_library
                RESTART IDENTITY CASCADE
                """
            )
        conn.commit()


def apply_schema(conn) -> None:
    """Apply the substrate schema to a connection. Idempotent.

    Reads schema.sql from the same directory and runs every CREATE.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    sql_path = os.path.join(here, "schema.sql")
    with open(sql_path, "r", encoding="utf-8") as fh:
        sql = fh.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
