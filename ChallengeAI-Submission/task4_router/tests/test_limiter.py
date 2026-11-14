
"""Functional and concurrency tests for the SQLite limiter."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from task4_router.limiter import TokenRateLimiter


class FakeClock:
    """Deterministic clock for sliding-window tests."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        # Makes the object usable as a callable clock.
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    """Provide a fresh fake clock for each test."""

    return FakeClock()


@pytest.fixture
def limiter(tmp_path, clock):
    """Create an isolated, on-disk database for each test."""

    instance = TokenRateLimiter(
        database_path=tmp_path / "test.sqlite3",
        token_limit=50_000,
        window_seconds=60,
        clock=clock,
    )

    instance.initialize()

    return instance


# ---------------------------------------------------------
# Core admission and isolation
# ---------------------------------------------------------


def test_admission_and_exact_limit(limiter):
    """Accept requests up to and including the token limit."""

    first = limiter.reserve("tenant-a", 30_000)
    second = limiter.reserve("tenant-a", 20_000)

    assert first.allowed
    assert first.request_id is not None
    assert first.remaining_tokens == 20_000

    assert second.allowed
    assert second.request_id is not None
    assert second.remaining_tokens == 0

    # Exactly 50,000 tokens must be accepted.
    assert limiter.get_usage("tenant-a") == 50_000

    # One additional token must be rejected.
    rejected = limiter.reserve("tenant-a", 1)

    assert not rejected.allowed
    assert rejected.request_id is None
    assert rejected.used_tokens == 50_000
    assert rejected.remaining_tokens == 0

    # Rejection must not change recorded usage.
    assert limiter.get_usage("tenant-a") == 50_000


def test_single_request_larger_than_budget(limiter):
    """Reject oversized reservations without consuming quota."""

    result = limiter.reserve("tenant-a", 50_001)

    assert not result.allowed
    assert result.request_id is None
    assert result.used_tokens == 0
    assert result.remaining_tokens == 50_000

    assert limiter.get_usage("tenant-a") == 0


def test_tenant_isolation(limiter):
    """One tenant's budget must not affect another tenant."""

    first = limiter.reserve("tenant-a", 50_000)
    second = limiter.reserve("tenant-b", 10_000)

    assert first.allowed
    assert second.allowed

    assert limiter.get_usage("tenant-a") == 50_000
    assert limiter.get_usage("tenant-b") == 10_000
    assert limiter.get_usage("tenant-c") == 0

    assert not limiter.reserve("tenant-a", 1).allowed
    assert limiter.reserve("tenant-b", 1).allowed


# ---------------------------------------------------------
# Sliding-window expiration
# ---------------------------------------------------------


def test_sliding_window_expiration(limiter, clock):
    """Expire individual records at the exact 60s boundary."""

    # t = 1000
    assert limiter.reserve("tenant-a", 30_000).allowed

    # t = 1030
    clock.advance(30)
    assert limiter.reserve("tenant-a", 20_000).allowed

    assert limiter.get_usage("tenant-a") == 50_000

    # t = 1059: first reservation remains active.
    clock.advance(29)

    assert not limiter.reserve("tenant-a", 1).allowed
    assert limiter.get_usage("tenant-a") == 50_000

    # t = 1060: first reservation is exactly 60s old
    # and must expire. The second remains active.
    clock.advance(1)

    assert limiter.get_usage("tenant-a") == 20_000

    # Admission must succeed using the recovered capacity.
    result = limiter.reserve("tenant-a", 5_000)

    assert result.allowed
    assert result.used_tokens == 25_000
    assert result.remaining_tokens == 25_000

    # Verify actual database eviction, not merely exclusion
    # of expired events from the usage query.
    with sqlite3.connect(limiter.database_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM token_events"
        ).fetchone()[0]

    assert count == 2

    # t = 1090: second reservation now expires.
    clock.advance(30)

    assert limiter.get_usage("tenant-a") == 5_000


# ---------------------------------------------------------
# Reservation reconciliation
# ---------------------------------------------------------


def test_reconcile_releases_unused_tokens(limiter, clock):
    """Replace a reservation with lower actual usage."""

    result = limiter.reserve("tenant-a", 10_000)

    assert result.allowed
    assert result.request_id is not None

    # Provider reports actual usage 20 seconds later.
    clock.advance(20)

    updated = limiter.reconcile(
        result.request_id,
        4_000,
    )

    assert updated
    assert limiter.get_usage("tenant-a") == 4_000

    # Released capacity is immediately available.
    extra = limiter.reserve("tenant-a", 46_000)

    assert extra.allowed
    assert limiter.get_usage("tenant-a") == 50_000

    # At t = 1060, the original reservation expires.
    # Reconciliation must not have reset its timestamp.
    clock.advance(40)

    assert limiter.get_usage("tenant-a") == 46_000


def test_reconcile_higher_actual_usage(limiter):
    """Account for actual usage even if it exceeds the limit."""

    result = limiter.reserve("tenant-a", 50_000)

    assert result.allowed
    assert result.request_id is not None

    # Actual consumption was higher than the reservation.
    assert limiter.reconcile(
        result.request_id,
        51_000,
    )

    assert limiter.get_usage("tenant-a") == 51_000

    # Further admission must be rejected.
    rejected = limiter.reserve("tenant-a", 1)

    assert not rejected.allowed
    assert rejected.remaining_tokens == 0
    assert rejected.used_tokens == 51_000


def test_reconcile_expired_reservation(limiter, clock):
    """An expired reservation must never be resurrected."""

    result = limiter.reserve("tenant-a", 10_000)

    assert result.allowed
    assert result.request_id is not None

    clock.advance(60)

    # A late completion cannot restore an expired event.
    updated = limiter.reconcile(
        result.request_id,
        5_000,
    )

    assert not updated
    assert limiter.get_usage("tenant-a") == 0

    # An unknown reservation also produces no update.
    assert not limiter.reconcile("unknown-request", 500)

    assert limiter.get_usage("tenant-a") == 0


# ---------------------------------------------------------
# Persistence
# ---------------------------------------------------------


def test_persistence_across_instances(tmp_path, clock):
    """A new limiter instance must preserve existing usage."""

    database_path = tmp_path / "persistent.sqlite3"

    first = TokenRateLimiter(
        database_path=database_path,
        clock=clock,
    )

    first.initialize()

    result = first.reserve("tenant-a", 40_000)

    assert result.allowed
    assert database_path.exists()

    # Create an entirely new limiter instance pointing
    # to the same database file.
    restarted = TokenRateLimiter(
        database_path=database_path,
        clock=clock,
    )

    restarted.initialize()

    # Initialization must not erase existing records.
    assert restarted.get_usage("tenant-a") == 40_000

    assert not restarted.reserve(
        "tenant-a",
        11_000,
    ).allowed

    assert restarted.reserve(
        "tenant-a",
        10_000,
    ).allowed

    assert restarted.get_usage("tenant-a") == 50_000

    # Persisted reservations must still expire normally.
    clock.advance(60)

    assert restarted.get_usage("tenant-a") == 0


# ---------------------------------------------------------
# Concurrent admission
# ---------------------------------------------------------


def test_concurrent_reservations_cannot_exceed_limit(
    tmp_path,
    clock,
):
    """Competing writers must not over-admit token requests."""

    database_path = tmp_path / "concurrency.sqlite3"

    initial = TokenRateLimiter(
        database_path=database_path,
        clock=clock,
    )

    initial.initialize()

    # Leave only 10,000 tokens available.
    assert initial.reserve(
        "tenant-a",
        40_000,
    ).allowed

    # Two independent limiter instances share the same
    # database, but not a Python connection or mutex.
    worker_a = TokenRateLimiter(
        database_path=database_path,
        clock=clock,
    )

    worker_b = TokenRateLimiter(
        database_path=database_path,
        clock=clock,
    )

    # Both workers must reach this point before either
    # begins its reservation attempt.
    barrier = Barrier(2)

    def attempt_reservation(worker):
        barrier.wait(timeout=5)

        return worker.reserve(
            "tenant-a",
            8_000,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_a = executor.submit(
            attempt_reservation,
            worker_a,
        )

        future_b = executor.submit(
            attempt_reservation,
            worker_b,
        )

        result_a = future_a.result(timeout=10)
        result_b = future_b.result(timeout=10)

    results = [result_a, result_b]

    # Exactly one competing writer should be admitted.
    assert sum(result.allowed for result in results) == 1

    allowed = next(
        result for result in results if result.allowed
    )

    rejected = next(
        result for result in results if not result.allowed
    )

    assert allowed.request_id is not None
    assert rejected.request_id is None

    assert allowed.used_tokens == 48_000
    assert rejected.used_tokens == 48_000

    assert allowed.remaining_tokens == 2_000
    assert rejected.remaining_tokens == 2_000

    # Confirm the final persistent database state.
    assert initial.get_usage("tenant-a") == 48_000

# ---------------------------------------------------------
# Database busy-timeout test
# ---------------------------------------------------------

def test_database_lock_failure_does_not_admit(
    tmp_path,
    clock,
):
    """A database lock failure must not bypass the limiter."""

    database_path = tmp_path / "locked.sqlite3"

    limiter = TokenRateLimiter(
        database_path=database_path,
        clock=clock,

        # Short timeout keeps this test fast.
        busy_timeout=0.05,
    )

    limiter.initialize()

    blocker = sqlite3.connect(
        database_path,
        isolation_level=None,
    )

    try:
        # Hold a write transaction using another connection.
        blocker.execute("BEGIN IMMEDIATE")

        # The limiter must fail when it cannot obtain its
        # own write transaction.
        with pytest.raises(sqlite3.OperationalError):
            limiter.reserve("tenant-a", 10_000)

    finally:
        blocker.rollback()
        blocker.close()

    # The failed admission must not have written a record.
    assert limiter.get_usage("tenant-a") == 0

    # Normal service should recover after the lock releases.
    result = limiter.reserve("tenant-a", 10_000)

    assert result.allowed
    assert limiter.get_usage("tenant-a") == 10_000
