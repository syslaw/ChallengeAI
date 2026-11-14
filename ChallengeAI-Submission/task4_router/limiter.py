
"""Persistent, token-aware sliding-window rate limiter."""

import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from task4_router.config import (
    DATABASE_PATH,
    SQLITE_BUSY_TIMEOUT_SECONDS,
    TOKEN_LIMIT,
    WINDOW_SECONDS,
)


@dataclass(frozen=True)
class ReservationResult:
    """Result of an atomic token admission attempt."""

    allowed: bool
    request_id: str | None
    used_tokens: int
    remaining_tokens: int


class TokenRateLimiter:
    """SQLite-backed rolling token budget for each tenant."""

    def __init__(
        self,
        database_path: Path | str = DATABASE_PATH,
        token_limit: int = TOKEN_LIMIT,
        window_seconds: float = WINDOW_SECONDS,
        busy_timeout: float = SQLITE_BUSY_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        # Store the database path and limiter configuration.
        self.database_path = Path(database_path)
        self.token_limit = token_limit
        self.window_seconds = window_seconds
        self.busy_timeout = busy_timeout

        # Injecting a clock lets tests advance time
        # without actually waiting for records to expire.
        self.clock = clock

        if token_limit <= 0:
            raise ValueError("Token limit must be positive.")

        if window_seconds <= 0:
            raise ValueError("Window must be positive.")

        if busy_timeout <= 0:
            raise ValueError("Busy timeout must be positive.")

    def _connect(self) -> sqlite3.Connection:
        """Open a connection scoped to one database operation."""

        return sqlite3.connect(
            str(self.database_path),

            # Bound how long SQLite waits for a write lock.
            timeout=self.busy_timeout,

            # Disable implicit transactions. We explicitly
            # control BEGIN, COMMIT, and ROLLBACK.
            isolation_level=None,
        )

    def initialize(self) -> None:
        """Create the persistent database schema."""

        # closing() guarantees the connection is closed.
        # A normal SQLite context manager alone does not
        # close the connection when leaving its block.
        with closing(self._connect()) as connection:

            # WAL allows readers to continue while another
            # connection holds a write transaction.
            connection.execute("PRAGMA journal_mode=WAL")

            connection.execute("BEGIN IMMEDIATE")

            try:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS token_events (
                        request_id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL,
                        timestamp REAL NOT NULL,
                        tokens INTEGER NOT NULL
                            CHECK (tokens >= 0)
                    )
                    """
                )

                # Efficient per-tenant sliding-window lookups.
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                        idx_token_events_tenant_time
                    ON token_events (tenant_id, timestamp)
                    """
                )

                # Efficient global expiration cleanup.
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                        idx_token_events_timestamp
                    ON token_events (timestamp)
                    """
                )

                connection.commit()

            except Exception:
                connection.rollback()
                raise

    def _evict_expired(
        self,
        connection: sqlite3.Connection,
        cutoff: float,
    ) -> None:
        """Delete events at or before the expiration boundary."""

        connection.execute(
            """
            DELETE FROM token_events
            WHERE timestamp <= ?
            """,
            (cutoff,),
        )

    def _sum_usage(
        self,
        connection: sqlite3.Connection,
        tenant_id: str,
        cutoff: float,
    ) -> int:
        """Calculate active token usage for one tenant."""

        row = connection.execute(
            """
            SELECT COALESCE(SUM(tokens), 0)
            FROM token_events
            WHERE tenant_id = ?
              AND timestamp > ?
            """,
            (tenant_id, cutoff),
        ).fetchone()

        return int(row[0])

    def reserve(
        self,
        tenant_id: str,
        tokens: int,
    ) -> ReservationResult:
        """Atomically check quota and reserve requested tokens."""

        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("Tenant ID must not be blank.")

        # Reject floats and booleans as well as invalid counts.
        if type(tokens) is not int or tokens <= 0:
            raise ValueError("Requested tokens must be positive integers.")

        with closing(self._connect()) as connection:

            # Acquire the SQLite write transaction BEFORE
            # checking quota. Competing writers must wait.
            connection.execute("BEGIN IMMEDIATE")

            try:
                # Capture time after acquiring the lock.
                # Time spent waiting must not make our
                # admission timestamp stale.
                now = self.clock()
                cutoff = now - self.window_seconds

                # Eviction, usage lookup, quota check, and
                # reservation are one atomic operation.
                self._evict_expired(connection, cutoff)

                used_tokens = self._sum_usage(
                    connection,
                    tenant_id,
                    cutoff,
                )

                # A rejected request must not create a
                # reservation or consume additional tokens.
                if used_tokens + tokens > self.token_limit:
                    result = ReservationResult(
                        allowed=False,
                        request_id=None,
                        used_tokens=used_tokens,
                        remaining_tokens=max(
                            0,
                            self.token_limit - used_tokens,
                        ),
                    )

                else:
                    # Generate an internal identifier so the
                    # exact reservation can be reconciled later.
                    request_id = str(uuid4())

                    connection.execute(
                        """
                        INSERT INTO token_events (
                            request_id,
                            tenant_id,
                            timestamp,
                            tokens
                        )
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            request_id,
                            tenant_id,
                            now,
                            tokens,
                        ),
                    )

                    new_usage = used_tokens + tokens

                    result = ReservationResult(
                        allowed=True,
                        request_id=request_id,
                        used_tokens=new_usage,
                        remaining_tokens=(
                            self.token_limit - new_usage
                        ),
                    )

                # Commit before reporting an admission result.
                # Even on rejection, expired-row cleanup is saved.
                connection.commit()

                return result

            except Exception:
                connection.rollback()
                raise

    def reconcile(
        self,
        request_id: str,
        actual_tokens: int,
    ) -> bool:
        """Replace an active reservation with actual usage.

        Return True when an active reservation was updated.
        Return False when the reservation is missing or expired.
        """

        if not isinstance(request_id, str) or not request_id:
            raise ValueError("Request ID must not be empty.")

        if type(actual_tokens) is not int or actual_tokens < 0:
            raise ValueError("Actual tokens must be nonnegative integers.")

        with closing(self._connect()) as connection:

            connection.execute("BEGIN IMMEDIATE")

            try:
                cutoff = self.clock() - self.window_seconds

                self._evict_expired(connection, cutoff)

                # Update only an existing, active reservation.
                #
                # Do not reset the timestamp: reconciliation
                # must not extend the original quota window.
                #
                # Do not insert a missing record: an expired
                # reservation must never be resurrected.
                cursor = connection.execute(
                    """
                    UPDATE token_events
                    SET tokens = ?
                    WHERE request_id = ?
                      AND timestamp > ?
                    """,
                    (
                        actual_tokens,
                        request_id,
                        cutoff,
                    ),
                )

                updated = cursor.rowcount == 1

                connection.commit()

                return updated

            except Exception:
                connection.rollback()
                raise

    def get_usage(self, tenant_id: str) -> int:
        """Read a tenant's current rolling token usage."""

        with closing(self._connect()) as connection:
            cutoff = self.clock() - self.window_seconds

            # Expired records are excluded even if no new
            # reservation has triggered physical deletion.
            return self._sum_usage(
                connection,
                tenant_id,
                cutoff,
            )