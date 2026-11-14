
"""Inspect Task 4's persistent SQLite token accounting."""

import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone

from task4_router.config import (
    DATABASE_PATH,
    TOKEN_LIMIT,
    WINDOW_SECONDS,
)


def main() -> None:
    """Print stored reservations and active tenant usage."""

    # Capture one timestamp so all calculations use
    # the same sliding-window boundary.
    now = time.time()
    cutoff = now - WINDOW_SECONDS

    # Open the existing database in read-only mode.
    # Inspection must never create or modify reservations.
    database_uri = (
        DATABASE_PATH.resolve().as_uri() + "?mode=ro"
    )

    with closing(
        sqlite3.connect(database_uri, uri=True)
    ) as connection:

        # Return rows with named columns.
        connection.row_factory = sqlite3.Row

        rows = connection.execute(
            """
            SELECT
                request_id,
                tenant_id,
                timestamp,
                tokens
            FROM token_events
            ORDER BY timestamp, request_id
            """
        ).fetchall()

        print("Database:", DATABASE_PATH)
        print("Stored records:", len(rows))
        print()

        # Track each tenant's active usage independently.
        active_usage = {}

        for row in rows:
            tenant = row["tenant_id"]
            timestamp = row["timestamp"]
            tokens = row["tokens"]

            # Match the limiter's exact expiration rule.
            active = timestamp > cutoff

            # Display timestamps in a readable UTC format.
            admitted_at = datetime.fromtimestamp(
                timestamp,
                tz=timezone.utc,
            ).isoformat(timespec="seconds")

            age = now - timestamp

            if tenant not in active_usage:
                active_usage[tenant] = 0

            if active:
                active_usage[tenant] += tokens

            print(
                f"Request: {row['request_id'][:8]}... | "
                f"Tenant: {tenant} | "
                f"Tokens: {tokens:,} | "
                f"Age: {age:.1f}s | "
                f"Status: {'ACTIVE' if active else 'EXPIRED'}"
            )

            print(f"  Admitted: {admitted_at}")

        print()
        print("Active tenant usage:")

        # Always display our two demonstration tenants,
        # even if they have no database records.
        tenants = set(active_usage) | {
            "tenant-a",
            "tenant-b",
        }

        for tenant in sorted(tenants):
            used = active_usage.get(tenant, 0)
            remaining = max(0, TOKEN_LIMIT - used)

            print(
                f"  {tenant}: {used:,} / "
                f"{TOKEN_LIMIT:,} tokens "
                f"({remaining:,} remaining)"
            )


if __name__ == "__main__":
    main()
